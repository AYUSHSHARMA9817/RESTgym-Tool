"""Main testing strategy: nominal phase, then mutation/fuzz phase, on a budget.

Entry point:

    from restleague.engine.strategy import run
    result = run(ops, budget_seconds=3600, base_url="http://host:port")

``run`` is synchronous (it owns its own event loop). ``run_async`` is the
coroutine version. Both accept the spec component's functions via keyword args; when omitted,
the fallback implementations in ``engine.fallback`` are used, so the engine
works standalone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models import Mode, Operation, Request, Response
from .client import ApiClient, auth_from_env
from .faults import Bandit, FaultTracker
from .mutations import OPERATORS, SWEEP_PAYLOADS, injectable_slots, mutate, sweep_request
from .pool import ResourcePool

log = logging.getLogger("restleague.strategy")

# the spec component's hooks (resolved lazily so the engine does not hard-depend on spec/)
GenRequest = Callable[[Operation, ResourcePool, Mode], Request]


def _load_spec_hooks():
    try:
        from ..spec import parser, values  # type: ignore
        from ..spec.deps import order_operations  # type: ignore
        return parser.parse_spec, order_operations, values.generate_request
    except Exception:
        from . import fallback
        return fallback.parse_spec, fallback.order_operations, fallback.generate_request


@dataclass
class OpStats:
    op: Operation
    attempts: int = 0
    successes: int = 0        # 2XX count
    last_good: Optional[Request] = None
    good_samples: list = field(default_factory=list)   # recent 2XX requests to mutate
    statuses: set = field(default_factory=set)
    covered: bool = False     # got at least one 2XX
    reached: bool = False     # got any non-0 response
    timeouts: int = 0         # requests that timed out (slow / possible DoS)
    slow: int = 0             # requests slower than the slow threshold
    mut_timeouts: dict = field(default_factory=dict)  # mutation name -> timeout count

    def note_good(self, req: Request) -> None:
        self.good_samples.append(req)
        if len(self.good_samples) > 10:
            self.good_samples.pop(0)
        self.last_good = req


@dataclass
class RunResult:
    unique_5xx: int
    operations_covered: int
    operations_total: int
    total_requests: int
    transport_errors: int
    elapsed: float
    faults: dict
    coverage: list[str]
    uncovered: list[str]
    status_histogram: dict


class Strategy:
    def __init__(self, ops: list[Operation], base_url: str, *, budget_seconds: float = 3600,
                 workers: int = 6, nominal_fraction: float = 0.15, seed: Optional[int] = None,
                 generate_request: Optional[GenRequest] = None, client: Optional[ApiClient] = None,
                 fault_log: Optional[str] = None):
        self.ops = ops
        self.base_url = base_url
        self.budget = budget_seconds
        self.workers = max(1, workers)
        self.nominal_fraction = nominal_fraction
        # a response slower than this (seconds) counts as "slow"; ops that pile up
        # timeouts/slow responses get deprioritised. Tunable via SLOW_THRESHOLD.
        self.slow_threshold = float(os.environ.get("SLOW_THRESHOLD", "3.0"))
        self.rng = random.Random(seed)
        self.pool = ResourcePool(seed=seed)
        self.faults = FaultTracker(log_path=fault_log)
        self.stats: dict[str, OpStats] = {o.key: OpStats(o) for o in ops}
        self.status_hist: dict[int, int] = {}
        self._gen = generate_request
        self._client = client
        self._owns_client = client is None
        self._stop = asyncio.Event()
        self._deadline = 0.0
        self._sweep: asyncio.Queue = asyncio.Queue()   # (op, slot, payload, family) injection sweep items
        self._swept_ops: set = set()
        # op-selection bandit (reward = new coverage or new fault), mutation bandit (reward = new fault)
        self.op_bandit = Bandit([o.key for o in ops], c=0.7, seed=seed)
        self.mut_bandit = Bandit(list(OPERATORS), c=0.8, gamma=0.99, seed=seed)

    # ------------------------------------------------------------- helpers
    @property
    def time_left(self) -> float:
        return self._deadline - time.monotonic()

    def generate(self, op: Operation, mode: Mode) -> Request:
        if self._gen is None:
            _, _, self._gen = _load_spec_hooks()
        try:
            req = self._gen(op, self.pool, mode)
        except Exception as e:
            log.debug("generate_request failed for %s (%s); using fallback", op.key, e)
            from . import fallback
            req = fallback.generate_request(op, self.pool, mode)
        req.op_key = op.key
        return req

    async def execute(self, req: Request, op: Operation) -> Response:
        resp = await self._client.send(req)
        st = self.stats[op.key]
        st.attempts += 1
        self.status_hist[resp.status] = self.status_hist.get(resp.status, 0) + 1
        # slow / timeout accounting (drives the back-off so one slow endpoint
        # can't eat the whole budget — e.g. a ReDoS on a validated string field)
        if resp.status == 0 and resp.error and resp.error.startswith("timeout"):
            st.timeouts += 1
            if req.mutation:
                st.mut_timeouts[req.mutation] = st.mut_timeouts.get(req.mutation, 0) + 1
        elif resp.elapsed >= self.slow_threshold:
            st.slow += 1
        if resp.status:
            st.reached = True
        if resp.ok:
            st.successes += 1
            if not st.covered:
                st.covered = True
            st.note_good(req)
            try:
                self.pool.harvest(op, req, resp)
            except Exception as e:
                log.debug("harvest failed: %s", e)
        st.statuses.add(resp.status)
        if resp.is_5xx:
            self.faults.record(op.key, req, resp)
        return resp

    # ------------------------------------------------------------- phase 1
    async def nominal_phase(self, deadline: float) -> None:
        """Try to get a 2XX for every operation, in dependency order, retrying."""
        order = self.ops
        max_rounds = 6
        queue: asyncio.Queue = asyncio.Queue()

        async def worker():
            while True:
                try:
                    op, attempt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if time.monotonic() >= deadline or self._client.api_down:
                    queue.task_done()
                    continue
                mode = Mode.VALID
                req = self.generate(op, mode)
                resp = await self.execute(req, op)
                st = self.stats[op.key]
                if not resp.ok and attempt + 1 < max_rounds and not resp.is_5xx and resp.status not in (401, 403, 404):
                    # retry later with fresh values (pool may have filled in)
                    await queue.put((op, attempt + 1))
                queue.task_done()

        # seed the queue round by round so creators run before consumers
        for op in order:
            await queue.put((op, 0))
        # a couple of extra passes for operations still uncovered (ids may exist now)
        tasks = [asyncio.create_task(worker()) for _ in range(self.workers)]
        await queue.join()
        for t in tasks:
            t.cancel()
        # targeted re-try of still-uncovered reachable ops
        for _ in range(3):
            if time.monotonic() >= deadline:
                break
            todo = [s.op for s in self.stats.values() if not s.covered and s.op.method != "DELETE"]
            if not todo:
                break
            await asyncio.gather(*(self._retry_cover(op, deadline) for op in todo[:self.workers * 4]))

    async def _retry_cover(self, op: Operation, deadline: float) -> None:
        for _ in range(3):
            if time.monotonic() >= deadline or self.stats[op.key].covered:
                return
            resp = await self.execute(self.generate(op, Mode.VALID), op)
            if resp.ok:
                return

    # ------------------------------------------------------------- phase 2
    def _op_weight(self, key: str) -> float:
        st = self.stats[key]
        w = 1.0
        if st.covered:
            w += 1.0                      # we can build good requests to mutate
        if self.faults.hits_by_op.get(key):
            w += 1.5                      # already crashes -> likely more bugs nearby
        if not st.reached:
            w *= 0.3                      # unreachable, stop wasting budget
        if st.op.method in ("POST", "PUT", "PATCH", "DELETE"):
            w += 0.5                      # writes crash more than reads
        # back off ops that keep timing out or responding slowly: each such
        # request costs seconds, so they drain the budget disproportionately
        slow_hits = st.timeouts * 3 + st.slow
        if slow_hits:
            w *= 1.0 / (1.0 + 0.5 * slow_hits)
        return max(w, 0.02)

    def _slow_mutations(self, key: str) -> set:
        """Mutations that have repeatedly timed out on this op — avoid re-sending them."""
        st = self.stats[key]
        return {m for m, n in st.mut_timeouts.items() if n >= 2}

    def _choose_op(self) -> Operation:
        weights = {k: self._op_weight(k) for k in self.stats}
        key = self.op_bandit.select(weights)
        return self.stats[key].op

    def _base_request(self, op: Operation) -> tuple[Request, bool]:
        """A request to mutate: a known-good one if we have it, else a fresh valid one."""
        st = self.stats[op.key]
        if st.good_samples and self.rng.random() < 0.75:
            import copy
            return copy.deepcopy(self.rng.choice(st.good_samples)), True
        return self.generate(op, Mode.VALID), False

    def _enqueue_sweep(self, op: Operation) -> None:
        """Queue a systematic injection sweep (payloads × injectable slots) for a covered op."""
        if op.key in self._swept_ops:
            return
        st = self.stats[op.key]
        base = st.good_samples[-1] if st.good_samples else None
        if base is None:
            return
        self._swept_ops.add(op.key)
        import copy
        tmpl = copy.deepcopy(base)
        slots = injectable_slots(tmpl, op)
        if not slots:
            return
        for slot in slots:
            for family, payload in SWEEP_PAYLOADS:
                self._sweep.put_nowait((op, slot, payload, family))

    async def _run_sweep_item(self) -> bool:
        try:
            op, slot, payload, family = self._sweep.get_nowait()
        except asyncio.QueueEmpty:
            return False
        st = self.stats[op.key]
        # if this op already times out a lot, its remaining sweep probes would
        # each cost a full timeout — skip them (we still keep the op's fault).
        if st.timeouts >= 3:
            self._sweep.task_done()
            return True
        base = st.good_samples[-1] if st.good_samples else st.last_good
        if base is not None:
            req = sweep_request(base, op, slot, payload, family)
            if req is not None:
                req.op_key = op.key
                before = self.faults.unique
                await self.execute(req, op)
                if self.faults.unique > before:
                    self.op_bandit.update(op.key, 1.0)
        self._sweep.task_done()
        return True

    async def mutation_phase(self, deadline: float) -> None:
        sem = asyncio.Semaphore(self.workers)
        # seed a systematic injection sweep for every operation we covered
        for st in list(self.stats.values()):
            if st.covered:
                self._enqueue_sweep(st.op)

        async def one():
            async with sem:
                if time.monotonic() >= deadline:
                    return
                if self._client.api_down:
                    await self._client.wait_until_alive(deadline)
                    return
                # drain the injection sweep preferentially (bounded, high-signal)
                if not self._sweep.empty() and self.rng.random() < 0.5:
                    if await self._run_sweep_item():
                        return
                op = self._choose_op()
                # a newly-covered op may not have been swept yet
                if self.stats[op.key].covered and op.key not in self._swept_ops:
                    self._enqueue_sweep(op)
                before_cov = self.stats[op.key].covered
                before_faults = self.faults.unique
                base, from_good = self._base_request(op)
                roll = self.rng.random()
                if not from_good and roll < 0.25:
                    # explore uncovered op with boundary/invalid whole-request generation
                    mode = self.rng.choice([Mode.BOUNDARY, Mode.INVALID, Mode.VALID])
                    req = self.generate(op, mode)
                    req.mutation = f"gen_{mode.value}"
                    mname = f"gen_{mode.value}"
                else:
                    avoid = self._slow_mutations(op.key)
                    mname = self.mut_bandit.select()
                    if mname in avoid:           # this mutation keeps timing out here
                        alt = [m for m in OPERATORS if m not in avoid]
                        mname = self.rng.choice(alt) if alt else mname
                    stack = 1 if self.rng.random() < 0.7 else 2
                    keep_path = from_good and base.path_params and self.rng.random() < 0.6
                    good_path = dict(base.path_params) if keep_path else None
                    req = mutate(mname, base, op, self.pool, self.rng, stack=stack)
                    if req is None:
                        req = self.generate(op, self.rng.choice([Mode.BOUNDARY, Mode.INVALID]))
                        req.mutation = mname = "gen_fuzz"
                    elif keep_path and mname not in ("bad_id", "swap_id"):
                        # keep the resource id valid so body/query bugs are reachable
                        req.path_params.update(good_path)
                req.op_key = op.key
                resp = await self.execute(req, op)
                # rewards
                new_fault = self.faults.unique > before_faults
                new_cov = self.stats[op.key].covered and not before_cov
                reward = 1.0 if new_fault else (0.5 if new_cov else (0.05 if resp.is_5xx else 0.0))
                self.op_bandit.update(op.key, reward)
                if mname in OPERATORS:
                    self.mut_bandit.update(mname, 1.0 if new_fault else (0.02 if resp.status else 0.0))

        last_log = time.monotonic()
        while time.monotonic() < deadline and not self._stop.is_set():
            await asyncio.gather(*(one() for _ in range(self.workers)))
            if time.monotonic() - last_log > 30:
                log.info("t=%.0fs  requests=%d  unique5xx=%d  covered=%d/%d  pool=%d",
                         self.budget - self.time_left, self._client.total_requests, self.faults.unique,
                         self._covered_count(), len(self.ops), self.pool.size())
                last_log = time.monotonic()

    # --------------------------------------------------------------- driver
    def _covered_count(self) -> int:
        return sum(1 for s in self.stats.values() if s.covered)

    async def run_async(self) -> RunResult:
        if self._client is None:
            self._client = ApiClient(self.base_url, auth=auth_from_env(),
                                     timeout=float(os.environ.get("REQUEST_TIMEOUT", "10")))
        start = time.monotonic()
        self._deadline = start + self.budget
        if not await self._client.probe():
            # don't give up: the API might just lack GET / ; wait briefly
            await self._client.wait_until_alive(min(self._deadline, start + 60))
        nominal_deadline = start + self.budget * self.nominal_fraction
        try:
            await self.nominal_phase(min(nominal_deadline, self._deadline))
            log.info("nominal done: covered %d/%d in %.0fs", self._covered_count(), len(self.ops),
                     time.monotonic() - start)
            await self.mutation_phase(self._deadline)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("strategy crashed: %s", e)
        finally:
            if self._owns_client:
                await self._client.close()
        return self._result(time.monotonic() - start)

    def _result(self, elapsed: float) -> RunResult:
        covered = [k for k, s in self.stats.items() if s.covered]
        uncovered = [k for k, s in self.stats.items() if not s.covered]
        return RunResult(
            unique_5xx=self.faults.unique,
            operations_covered=len(covered),
            operations_total=len(self.ops),
            total_requests=self._client.total_requests,
            transport_errors=self._client.total_transport_errors,
            elapsed=elapsed,
            faults=self.faults.summary(),
            coverage=sorted(covered),
            uncovered=sorted(uncovered),
            status_histogram={str(k): v for k, v in sorted(self.status_hist.items())},
        )


def run(ops: list[Operation], budget_seconds: float = 3600, base_url: str = "http://localhost:8080",
        *, workers: int = 6, seed: Optional[int] = None, generate_request: Optional[GenRequest] = None,
        nominal_fraction: float = 0.15, fault_log: Optional[str] = None) -> RunResult:
    strat = Strategy(ops, base_url, budget_seconds=budget_seconds, workers=workers, seed=seed,
                     generate_request=generate_request, nominal_fraction=nominal_fraction, fault_log=fault_log)
    return asyncio.run(strat.run_async())
