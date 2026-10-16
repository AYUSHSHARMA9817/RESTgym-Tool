"""Track A runner: stateful sequence fuzzing with metamorphic verification.

This is the opt-in entry point for the architecture in
docs/architecture/track_a_stateful_fuzzing.md. It is **not** the competition
pipeline: ``restleague.main`` runs ``engine.strategy.run`` unless ``TRACK_A``
is set in the environment, so the submission validated against RESTgym is
unaffected by anything here.

    TRACK_A=1 HOST=127.0.0.1 PORT=8080 API=pet-clinic TIME_BUDGET=5 \
        SPEC=specs/pet-clinic-openapi.json python3 -m restleague.main

The loop, per iteration:

    scheduler  -> pick a target operation and (maybe) a structural mutation
    synthesizer-> plan the setup prefix that target needs, skipping what the
                  graph already has
    mutator    -> reshape the plan (omit / invert / duplicate / cross-entity /
                  illegal transition)
    executor   -> run it, binding related ids between steps, feeding the graph
                  and the state-machine inferrer
    oracles    -> check the executed sequence for semantic bugs
    scheduler  -> reward what produced coverage, transitions, violations, 5XXs

Faults are counted by the engine's existing FaultTracker so the 5XX numbers
stay comparable with the default pipeline; semantic findings go to a separate
ViolationTracker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .engine.client import ApiClient, auth_from_env
from .engine.faults import FaultTracker
from .engine.pool import ResourcePool, resource_for_param
from .models import Mode, Operation, Sequence, Step
from .oracles import ALL_ORACLES
from .oracles.base import OracleContext
from .findings import FindingStore
from .oracles.tracker import ViolationTracker
from .sequence.executor import AsyncSequenceExecutor
from .sequence.mutator import SequenceMutator
from .sequence.scheduler import Outcome, SequenceScheduler, Weights
from .sequence.synthesizer import SequenceSynthesizer
from .state.graph import RelationalKnowledgeGraph
from .state.inferrer import DynamicStateMachineInferrer

log = logging.getLogger("restleague.track_a")


@dataclass
class TrackAResult:
    unique_5xx: int
    unique_violations: int
    operations_covered: int
    operations_total: int
    sequences_run: int
    sequence_errors: int
    unreproduced: int
    total_requests: int
    elapsed: float
    faults: dict = field(default_factory=dict)
    violations: dict = field(default_factory=dict)
    coverage: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    states: dict = field(default_factory=dict)
    scheduler: dict = field(default_factory=dict)

    def describe(self) -> str:
        errs = f" errors={self.sequence_errors}" if self.sequence_errors else ""
        unrep = f" unreproduced={self.unreproduced}" if self.unreproduced else ""
        return (f"sequences={self.sequences_run}{errs} requests={self.total_requests} "
                f"unique5xx={self.unique_5xx} violations={self.unique_violations} "
                f"covered={self.operations_covered}/{self.operations_total}{unrep} "
                f"in {self.elapsed:.0f}s")


class TrackARunner:
    def __init__(self, ops: list[Operation], base_url: str, *, budget_seconds: float = 3600,
                 seed: Optional[int] = None, generate_request: Optional[Callable] = None,
                 client: Optional[Any] = None, use_oracles: bool = True,
                 weights: Optional[Weights] = None, use_pool: bool = True,
                 max_checks_per_op: Optional[int] = None, workers: int = 1,
                 findings_file: Optional[str] = None, api: str = "",
                 unauth_client: Any = None, alt_client: Any = None):
        self.ops = ops
        self.base_url = base_url
        self.budget = budget_seconds
        self.seed = seed
        self._client = client
        self._owns_client = client is None
        self.use_oracles = use_oracles

        if generate_request is None:
            from .spec.values import generate_request as gen
            generate_request = gen
        self.generate_request = generate_request

        self.rkg = RelationalKnowledgeGraph(seed=seed)
        self.dsmi = DynamicStateMachineInferrer()
        self.pool = ResourcePool(seed=seed) if use_pool else None
        self.synth = SequenceSynthesizer(ops)
        self.mutator = SequenceMutator(ops, seed=seed)
        targets = self.synth.targets() or ops
        self.scheduler = SequenceScheduler(targets, seed=seed, weights=weights,
                                           static_priors=self.synth.target_priors())
        self.faults = FaultTracker()
        self.violations = ViolationTracker()
        # Every recorded violation keeps the requests that produced it, so it
        # can be replayed against a clean API and checked by someone who did
        # not watch the run. Written out as findings arrive, not at the end.
        self.findings = FindingStore(findings_file, api=api)
        self._unauth_client = unauth_client   # no-credential client (missing-auth oracle)
        self._alt_client = alt_client         # second-identity client (cross-user oracle)
        self.workers = max(1, workers)
        self.active_workers = self.workers   # the allocator may resize this live
        self.oracles = [cls() for cls in ALL_ORACLES] if use_oracles else []
        if self.workers > 1 and self.oracles:
            # Concurrent sequences mutate the API while an oracle is mid-way
            # through comparing two observations, which is exactly how a
            # correct API gets reported as broken (see _check_step). Oracles
            # that judge a single response are unaffected, so only those stay
            # on once more than one sequence is in flight.
            stood_down = [o.name for o in self.oracles if o.active]
            self.oracles = [o for o in self.oracles if not o.active]
            if stood_down:
                log.info("workers=%d: comparison oracles stood down (%s)",
                         self.workers, ", ".join(stood_down))
        self.ops_by_key = {o.key: o for o in ops}
        self.sequences_run = 0
        self.sequence_errors = 0
        self.steps_sent = 0   # sequence steps issued (excludes oracle probes)
        self.unreproduced = 0 # findings dropped because a re-check did not confirm them
        self._seen_transitions: set = set()
        # how many times each oracle may check each operation (see _take_budget)
        self.max_checks_per_op = (max_checks_per_op if max_checks_per_op is not None
                                  else int(os.environ.get("ORACLE_CHECKS_PER_OP", "3")))
        # the cap above applies per window, not per run (see _take_budget).
        # Scaled to the budget so short runs still get several windows, and
        # clamped so an hour-long run re-checks every few minutes rather than
        # once or constantly.
        self.oracle_window = float(os.environ.get(
            "ORACLE_WINDOW_SECONDS", max(15.0, min(300.0, budget_seconds / 12.0))))
        self._oracle_checks: dict[tuple, int] = {}
        self._oracle_window_start = time.monotonic()
        self._history: list = []
        self._taint: dict = {}   # resource type -> stored injection payloads

    # ------------------------------------------------------------- the loop
    async def run_async(self) -> TrackAResult:
        if self._client is None:
            self._client = ApiClient(self.base_url, auth=auth_from_env(),
                                     timeout=float(os.environ.get("REQUEST_TIMEOUT", "10")))
        executor = AsyncSequenceExecutor(self._client, self.generate_request,
                                         pool=self.pool, seed=self.seed)
        ctx = OracleContext(ops=self.ops_by_key, client=self._client, rkg=self.rkg,
                            unauth_client=self._unauth_client, alt_client=self._alt_client,
                            taint=self._taint)

        start = time.monotonic()
        deadline = start + self.budget
        try:
            await asyncio.gather(*(self._worker(i, executor, ctx, deadline, start)
                                   for i in range(self.workers)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("track A crashed: %s", e)
        finally:
            if self._owns_client:
                await self._client.close()
        return self._result(time.monotonic() - start)

    async def _worker(self, idx: int, executor, ctx: OracleContext,
                      deadline: float, start: float) -> None:
        """One sequence at a time; ``workers`` of these run concurrently.

        ``idx`` lets the allocator resize the pool live: a worker whose index
        has fallen outside ``active_workers`` idles instead of exiting, so
        capacity can be handed back without respawning tasks.
        """
        last_log = start
        while time.monotonic() < deadline:
            if idx >= self.active_workers:
                await asyncio.sleep(0.2)
                continue
            try:
                target = self.scheduler.next_target()
                if target is None:
                    return
                mutation_name = self.scheduler.next_mutation()
                attack_name = self.scheduler.next_attack()
                before = self._snapshot()
                seq_started = time.monotonic()
                try:
                    plan = self.synth.synthesize(target, rkg=self.rkg)
                    mutation = None
                    if mutation_name:
                        mutation = self.mutator.mutate(plan, name=mutation_name,
                                                       dsmi=self.dsmi, rkg=self.rkg)
                    seq = await executor.execute(
                        mutation or plan, self.rkg, dsmi=self.dsmi,
                        target_mode=Mode.VALID,
                        stop_on_prefix_failure=(mutation is None
                                                or mutation.name != "omit_prerequisite"),
                        on_step=self._make_step_hook(ctx), attack=attack_name)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # One bad sequence must not end an hour-long run. Deprioritise
                    # this target and keep going rather than abandoning the budget.
                    self.sequence_errors += 1
                    log.debug("sequence for %s failed: %s", target.key, e)
                    self.scheduler.update(target, mutation_name,
                                          Outcome(prefix_failed=True), attack=attack_name)
                    continue
                self.sequences_run += 1
                self._account(seq)
                self.scheduler.note_cost(target.key, time.monotonic() - seq_started)
                outcome = self._outcome(before, seq)
                self.scheduler.update(target, mutation_name, outcome, attack=attack_name)

                if idx == 0 and time.monotonic() - last_log > 30:
                    log.info("t=%.0fs %s", time.monotonic() - start,
                             self._result(time.monotonic() - start).describe())
                    last_log = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # keep this worker alive; the others are unaffected
                self.sequence_errors += 1
                log.debug("worker %d iteration failed: %s", idx, e)

    # ------------------------------------------------------------ bookkeeping
    def _snapshot(self) -> tuple:
        return (self.faults.unique, self.violations.unique,
                len(self.scheduler.covered), len(self._seen_transitions))

    def _account(self, seq: Sequence) -> None:
        """Record coverage, 5XXs and newly observed transitions from one sequence."""
        from .oracles.secondorder import record_taint
        self.steps_sent += len(seq.steps)
        for step in seq.steps:
            op = self.ops_by_key.get(step.request.op_key)
            if op is not None:
                record_taint(self._taint, op, step.request, step.response)
            resp, req = step.response, step.request
            if resp is None:
                continue
            if resp.is_5xx:
                self.faults.record(req.op_key, req, resp)
            if resp.ok:
                self.scheduler.mark_covered(req.op_key)
                # Never harvest from a request we deliberately corrupted. The
                # pool feeds later request generation, so an accepted attack
                # payload -- an injection string, a boundary value, the
                # mass-assignment oracle's "role": "superadmin" -- would be
                # reused as if it were a realistic value. Measured on
                # gestao-hospital, a pool polluted this way pushed the engine
                # from 63 of 145 responses being 2XX to 28 of 236, and its
                # coverage from 17/20 to 5/20.
                if self.pool is not None and not req.mutation:
                    op = self.ops_by_key.get(req.op_key)
                    if op is not None:
                        try:
                            self.pool.harvest(op, req, resp)
                        except Exception as e:
                            log.debug("pool harvest failed: %s", e)
        for etype in list(self.dsmi.summary()):
            for (frm, op_key), tos in self.dsmi.transitions(etype).items():
                for to in tos:
                    self._seen_transitions.add((etype, frm, op_key, to))

    def _make_step_hook(self, ctx: OracleContext):
        """Returns the per-step callback the executor awaits after every step."""
        history: list = []

        async def hook(step, op, is_last: bool) -> None:
            history.append(step)
            if self.oracles:
                await self._check_step(step, ctx, is_last, history)
        return hook

    async def _check_step(self, step, ctx: OracleContext, is_last: bool,
                          history: Optional[list] = None) -> None:
        """Run the oracles against one step, immediately after it executed.

        Checking here rather than over the finished sequence is what makes
        attribution correct: by the end of a sequence a later step may have
        changed the state an oracle re-reads, and the oracle would blame the
        earlier step for it. Mutating oracles still wait for the final step,
        since their probes would corrupt the steps that follow.
        """
        op_key = step.request.op_key
        one = Sequence(target_op_key=op_key)
        one.append(step)
        self._history = history or [step]
        for oracle in self.oracles:
            if oracle.active and ctx.client is None:
                continue
            if oracle.mutating and not is_last:
                continue
            if not self._take_budget(oracle.name, op_key):
                continue
            try:
                found = await oracle.check(one, ctx)
                if found and ctx.verify:
                    found = await self._reproduce(oracle, one, ctx, found)
            except Exception as e:
                log.debug("oracle %s failed: %s", oracle.name, e)
                continue
            for v in found:
                if self.violations.record(v):
                    self.findings.record(v, self._reproduction(step))
                    log.info("[%s] %s -- %s", v.oracle, v.op_key, v.summary)

    async def _reproduce(self, oracle, one: Sequence, ctx: OracleContext,
                         found: list) -> list:
        """Re-run a check and keep only the findings that fire again.

        A finding that does not reproduce was a coincidence -- a value that
        happened to differ once, a request that happened to be slow. Checking
        twice and intersecting by signature costs one extra probe per finding
        and applies to every oracle at once, instead of patching each flaky
        case as it is discovered. It does not catch *deterministic* false
        positives, which is what the tolerance helpers in oracles/base.py are
        for; the two are complementary.
        """
        try:
            again = await oracle.check(one, ctx)
        except Exception as e:
            log.debug("oracle %s failed on re-check: %s", oracle.name, e)
            return []
        seen = {self.violations.signature(v) for v in again}
        confirmed = []
        for v in found:
            if self.violations.signature(v) in seen:
                v.evidence["reproduced"] = True
                confirmed.append(v)
            else:
                self.unreproduced += 1
                log.debug("[%s] %s did not reproduce: %s", v.oracle, v.op_key, v.summary)
        return confirmed

    def _reproduction(self, step) -> Sequence:
        """Everything needed to show this finding starting from nothing.

        Two parts. First the requests that created the entities this step acts
        on, which on a warm graph were issued by *earlier* sequences -- without
        them a replay on a clean API is just a 404. Then this sequence's own
        steps up to and including the offending one.
        """
        from .findings import RequestRecord

        seq = Sequence(target_op_key=step.request.op_key)
        here = [s for s in (self._history or [step])]
        if step in here:
            here = here[:here.index(step) + 1]
        already = {id(s.request) for s in here}

        seen: set = set()
        for name, value in step.request.path_params.items():
            etype = resource_for_param(step.request.path, name)
            if not etype:
                continue
            for rec in self.rkg.provenance(etype, value):
                marker = (rec.get("method"), rec.get("path"), json.dumps(rec.get("body"),
                                                                         default=str, sort_keys=True))
                if marker in seen:
                    continue
                seen.add(marker)
                req = RequestRecord(**rec).build()
                if id(req) in already:
                    continue
                seq.append(Step(request=req, response=None, bindings=dict(req.path_params)))
        for s in here:
            seq.append(s)
        return seq

    def _take_budget(self, oracle_name: str, op_key: str) -> bool:
        """Per-(oracle, operation) check cap, refreshed every window.

        Re-verifying that PUT /owners/{id} is idempotent on every one of
        thousands of sequences costs requests and tells us nothing new, so the
        count is capped. But a *lifetime* cap was far too strict: measured
        against a live pet-clinic, all 207 (oracle, operation) slots were spent
        inside the first ~81 of 8,345 sequences, after which no semantic check
        ran again. The oracles then only ever saw the API in its fresh,
        uninteresting early state and never in the richer states built up later
        in the run -- exactly where the state-dependent bugs live.

        So the budget is a rate, not a quota: it resets each window, letting
        the oracles re-sample as the API's state evolves while still bounding
        the traffic and the repetition.
        """
        now = time.monotonic()
        if now - self._oracle_window_start >= self.oracle_window:
            self._oracle_checks.clear()
            self._oracle_window_start = now
        key = (oracle_name, op_key)
        used = self._oracle_checks.get(key, 0)
        if used >= self.max_checks_per_op:
            return False
        self._oracle_checks[key] = used + 1
        return True

    def _outcome(self, before: tuple, seq: Sequence) -> Outcome:
        after = self._snapshot()
        prefix_failed = any(s.response is None or not s.response.ok for s in seq.steps[:-1])
        return Outcome(new_faults=after[0] - before[0],
                       new_violations=after[1] - before[1],
                       new_coverage=after[2] - before[2],
                       new_transitions=after[3] - before[3],
                       prefix_failed=prefix_failed)

    def _result(self, elapsed: float) -> TrackAResult:
        covered = sorted(self.scheduler.covered)
        uncovered = sorted(k for k in self.ops_by_key if k not in self.scheduler.covered)
        return TrackAResult(
            unique_5xx=self.faults.unique,
            unique_violations=self.violations.unique,
            operations_covered=len(covered),
            operations_total=len(self.ops),
            sequences_run=self.sequences_run,
            sequence_errors=self.sequence_errors,
            unreproduced=self.unreproduced,
            total_requests=getattr(self._client, "total_requests", 0),
            elapsed=elapsed,
            faults=self.faults.summary(),
            violations=self.violations.summary(),
            coverage=covered,
            uncovered=uncovered,
            states=self.dsmi.summary(),
            scheduler=self.scheduler.stats(),
        )


def run_track_a(ops: list[Operation], budget_seconds: float = 3600,
                base_url: str = "http://localhost:8080", **kwargs) -> TrackAResult:
    """Synchronous entry point, mirroring engine.strategy.run."""
    runner = TrackARunner(ops, base_url, budget_seconds=budget_seconds, **kwargs)
    return asyncio.run(runner.run_async())


def enabled(env=os.environ) -> bool:
    """Is Track A switched on? Default: no (the competition pipeline runs)."""
    return str(env.get("TRACK_A", "")).strip().lower() in ("1", "true", "yes", "on")
