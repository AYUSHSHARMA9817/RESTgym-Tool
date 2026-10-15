"""Hybrid mode: run the default engine and Track A together, sharing state.

Opt in with ``HYBRID=1``; ``restleague/main.py`` otherwise runs the pipeline
RESTgym validated. Nothing in ``engine/`` is modified -- ``Strategy`` already
accepts an injected client, and its ``pool``/``faults`` are plain attributes, so
the shared instances are wired in from outside.

Why both: measured against a live Spring Boot pet-clinic over 90 s, the default
engine found 384 unique 5XX and Track A 262, but only 115 signatures were common
to both. 119 were unique to the engine (mostly DELETE and item GET routes, where
its ~2x request rate wins) and 78 unique to Track A (nested writes such as
``POST /owners/{id}/pets/{id}/visits``, which need a valid parent chain to reach
at all). Neither dominates, so the union is worth more than either alone.

How they help each other: the engine's weakness is that it guesses ids, and a
guessed id mostly yields a 404 that never reaches business logic. Track A's
graph *knows* which ids exist and how they relate, and harvests them into the
shared ResourcePool -- so the engine draws from a pool of real, related ids.

The shape (design D): a quiet warm-up, then parallel.

    0 .. warmup      Track A alone, all oracles active
    warmup .. end    both engines concurrently, Track A passive oracles only

The warm-up exists because five of the eight oracles work by comparing two
observations (GET twice, PUT twice, write-then-read). Anything else mutating the
API in between makes them report bugs that are not there -- the same failure we
already hit and fixed once, where a later step's legitimate change was blamed on
an earlier one. During the warm-up nothing else is running, so those oracles are
trustworthy; once the engine joins, only the oracles that judge a single
response (``active = False``) stay on.

Concurrency uses asyncio, not threads: both engines are I/O-bound (~280 and ~580
requests/second against a real API, versus ~5,000 sequences/second against an
in-process fake), so one event loop already multiplexes them. Threads would add
lock contention and two loops for no throughput gain.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .engine.client import ApiClient, auth_from_env
from .engine.strategy import Strategy
from .models import Operation
from .track_a import TrackARunner

log = logging.getLogger("restleague.hybrid")

DEFAULT_WARMUP_FRACTION = 0.10
# "sequential": each half gets its own full budget, run one after the other.
# "parallel": both share a single budget (use when wall-clock time is capped).
DEFAULT_MODE = os.environ.get("HYBRID_MODE", "sequential")


@dataclass
class HybridResult:
    unique_5xx: int                 # deduped across both engines (shared FaultTracker)
    unique_violations: int
    operations_covered: int         # union of what either engine covered
    operations_total: int
    total_requests: int
    elapsed: float
    # Both engines share one ApiClient, so its counter is the combined total and
    # cannot be split exactly; Track A's sequence steps are counted directly and
    # the remainder covers the engine plus every oracle probe.
    track_a_steps: int = 0
    track_a_sequences: int = 0
    other_requests: int = 0
    coverage: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    faults: dict = field(default_factory=dict)
    violations: dict = field(default_factory=dict)

    def describe(self) -> str:
        return (f"HYBRID requests={self.total_requests} "
                f"(trackA={self.track_a_steps} engine={self.other_requests}) "
                f"unique5xx={self.unique_5xx} violations={self.unique_violations} "
                f"covered={self.operations_covered}/{self.operations_total} "
                f"in {self.elapsed:.0f}s")


async def _run_sequential(ops: list[Operation], budget_seconds: float, base_url: str, *,
                          seed: Optional[int] = None, workers: int = 6,
                          client: Optional[Any] = None,
                          track_workers: int = 1) -> HybridResult:
    """Track A for a full budget, then the engine for a full budget, unioned.

    Preferred when no single wall-clock budget has to cover both halves, which
    is the usual case outside a competition run. It is simpler and finds more
    than the parallel arrangement, for two reasons.

    Every oracle stays valid. Six of the eight conclude by comparing two
    observations, so concurrent traffic from the other half makes them report
    bugs that are not there; the parallel mode therefore stands them down for
    all but its warm-up. Running alone, Track A keeps the full set for its
    entire phase -- and on gestao-hospital those six produced most of the 14
    semantic findings.

    Neither half starves the other. Sharing an API (and previously a connection
    pool) let the engine's slow requests squeeze Track A down to 1,022 requests
    on one seed, against roughly 20,000 on the others.

    Track A runs first so the engine inherits a warm ResourcePool: the graph
    knows which ids exist and how they relate, and the engine would otherwise
    guess and collect 404s. That was the whole point of sharing state, and it
    never required the two to run at the same time.
    """
    own_clients = client is None
    timeout = float(os.environ.get("REQUEST_TIMEOUT", "10"))
    track_client = client or ApiClient(base_url, auth=auth_from_env(), timeout=timeout)
    engine_client = client or ApiClient(base_url, auth=auth_from_env(), timeout=timeout)

    runner = TrackARunner(ops, base_url, budget_seconds=budget_seconds, seed=seed,
                          client=track_client, workers=track_workers)
    strat = Strategy(ops, base_url, budget_seconds=budget_seconds, workers=workers,
                     seed=seed, client=engine_client)
    runner.pool = strat.pool          # filled by phase 1, consumed by phase 2

    start = time.monotonic()
    try:
        log.info("hybrid/sequential: phase 1 -- Track A, %.0fs, %d oracle(s)",
                 budget_seconds, len(runner.oracles))
        await runner.run_async()
        log.info("hybrid/sequential: phase 1 done -- %d sequences, %d unique 5XX, "
                 "%d violations, pool=%d, graph=%d",
                 runner.sequences_run, runner.faults.unique, runner.violations.unique,
                 strat.pool.size(), runner.rkg.size())

        log.info("hybrid/sequential: phase 2 -- engine, %.0fs, on the warm pool",
                 budget_seconds)
        await strat.run_async()
    finally:
        if own_clients:
            await track_client.close()
            await engine_client.close()
    return _merge(runner, strat, ops, time.monotonic() - start)


async def _allocate(strat: Strategy, runner: TrackARunner, deadline: float, *,
                    total: int, interval: float = 10.0) -> None:
    """Shift capacity toward whichever half is finding bugs.

    Without this the two halves simply compete for a fixed API throughput and
    hybrid converges on the *average* of them rather than the better one.
    Measured over three seeds on gestao-hospital, Track A averaged 8.7 unique
    5XX and the engine 1.0, yet hybrid landed at 6.0 -- and one seed collapsed
    to 2 because the engine's slow requests starved Track A. On pet-clinic the
    ranking is the other way round, so no fixed split is right for both.

    Productivity is new unique 5XX per request issued, measured per interval
    from each half's own client and fault tracker (which is why they are no
    longer shared). One unit of capacity moves per interval, and neither half
    can be squeezed below one worker, so a temporary bad patch cannot shut
    either of them out permanently.
    """
    prev = (0, 0, 0, 0)
    while time.monotonic() < deadline:
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            return
        e_f, e_q = strat.faults.unique, strat._client.total_requests
        t_f, t_q = runner.faults.unique, runner._client.total_requests
        rate_e = (e_f - prev[0]) / max(1, e_q - prev[1])
        rate_t = (t_f - prev[2]) / max(1, t_q - prev[3])
        prev = (e_f, e_q, t_f, t_q)

        if rate_e > rate_t and strat.workers < total - 1:
            strat.workers += 1
            runner.active_workers = max(1, runner.active_workers - 1)
        elif rate_t > rate_e and runner.active_workers < total - 1:
            runner.active_workers += 1
            strat.workers = max(1, strat.workers - 1)
        else:
            continue
        log.info("allocator: engine=%d trackA=%d (yield/req engine=%.5f trackA=%.5f)",
                 strat.workers, runner.active_workers, rate_e, rate_t)


async def run_hybrid_async(ops: list[Operation], budget_seconds: float, base_url: str, *,
                           seed: Optional[int] = None,
                           warmup_fraction: float = DEFAULT_WARMUP_FRACTION,
                           workers: int = 6, client: Optional[Any] = None,
                           adaptive: bool = True, mode: str = DEFAULT_MODE,
                           track_workers: int = 1) -> HybridResult:
    """Run both halves and union their findings.

    ``mode="sequential"`` (the default) gives each half its own full ``budget_seconds``
    one after the other; ``mode="parallel"`` runs them together inside a single
    budget. ``client``, when given, is used by both halves (tests only).
    """
    if mode == "sequential":
        return await _run_sequential(ops, budget_seconds, base_url, seed=seed,
                                     workers=workers, client=client,
                                     track_workers=track_workers)
    own_clients = client is None
    timeout = float(os.environ.get("REQUEST_TIMEOUT", "10"))
    # Separate clients: sharing one meant the two halves also shared a
    # connection pool, so the engine's slow requests could block Track A
    # entirely. They also give exact per-half request counts, which the
    # allocator needs.
    engine_client = client or ApiClient(base_url, auth=auth_from_env(), timeout=timeout)
    track_client = client or ApiClient(base_url, auth=auth_from_env(), timeout=timeout)

    warmup = max(0.0, min(budget_seconds, budget_seconds * warmup_fraction))
    remaining = max(0.0, budget_seconds - warmup)

    # Constructed with workers=1 on purpose: TrackARunner stands the comparison
    # oracles down as soon as workers > 1, and the warm-up exists precisely to
    # run them. The parallel phase raises the worker count afterwards.
    runner = TrackARunner(ops, base_url, budget_seconds=budget_seconds, seed=seed,
                          client=track_client, workers=1)
    strat = Strategy(ops, base_url, budget_seconds=remaining, workers=workers,
                     seed=seed, client=engine_client)

    # The pool stays shared -- that is the synergy. Track A's graph knows which
    # ids exist and how they relate, and the engine otherwise has to guess.
    # Fault trackers do not, so each half's yield can be measured; they are
    # unioned by signature at the end, which dedups exactly as one shared
    # tracker would have.
    runner.pool = strat.pool

    start = time.monotonic()
    try:
        if warmup > 0:
            log.info("hybrid: warm-up %.0fs (Track A alone, all oracles)", warmup)
            runner.budget, runner.workers, runner.active_workers = warmup, 1, 1
            await runner.run_async()
            log.info("hybrid: warm-up done -- pool=%d entities=%d",
                     strat.pool.size(), runner.rkg.size())

        # The engine is about to mutate state concurrently, so retire the
        # oracles that conclude by comparing two separate observations.
        dropped = [o.name for o in runner.oracles if o.active]
        runner.oracles = [o for o in runner.oracles if not o.active]
        log.info("hybrid: parallel phase %.0fs; passive oracles only (%s), stood down: %s",
                 remaining, ", ".join(o.name for o in runner.oracles) or "none",
                 ", ".join(dropped) or "none")

        if remaining > 0:
            runner.budget = remaining
            runner.workers = runner.active_workers = max(1, workers // 2)
            tasks = [runner.run_async(), strat.run_async()]
            if adaptive:
                tasks.append(_allocate(strat, runner, time.monotonic() + remaining,
                                       total=workers + runner.workers))
            await asyncio.gather(*tasks)
    finally:
        if own_clients:
            await engine_client.close()
            await track_client.close()

    elapsed = time.monotonic() - start
    return _merge(runner, strat, ops, elapsed)


def _merge(runner: TrackARunner, strat: Strategy, ops: list[Operation],
           elapsed: float) -> HybridResult:
    """Union both halves' coverage and fault signatures.

    Each half keeps its own FaultTracker so the allocator can tell them apart,
    so the combined unique count is the union of their signature sets -- the
    same number one shared tracker would have produced, since the signature is
    what defines a distinct fault.
    """
    engine_covered = {k for k, s in strat.stats.items() if s.covered}
    covered = sorted(engine_covered | set(runner.scheduler.covered))
    all_keys = {o.key for o in ops}

    engine_faults, track_faults = strat.faults.faults, runner.faults.faults
    merged = dict(engine_faults)
    merged.update(track_faults)
    summary = strat.faults.summary()
    summary["unique_5xx"] = len(merged)
    summary["faults"] = [{"status": f.status, "op": f.op, "msg": f.msg, "count": f.count,
                          "t": round(f.first_seen, 1), "mutation": f.example_mutation}
                         for f in merged.values()]
    summary["engine_only"] = len(set(engine_faults) - set(track_faults))
    summary["track_a_only"] = len(set(track_faults) - set(engine_faults))

    engine_requests = getattr(strat._client, "total_requests", 0)
    track_requests = getattr(runner._client, "total_requests", 0)
    if runner._client is strat._client:         # a single injected client (tests)
        total = engine_requests
        track_requests = min(runner.steps_sent, total)
        engine_requests = total - track_requests
    return HybridResult(
        unique_5xx=len(merged),
        unique_violations=runner.violations.unique,
        operations_covered=len(covered),
        operations_total=len(ops),
        total_requests=engine_requests + track_requests,
        elapsed=elapsed,
        track_a_steps=track_requests,
        track_a_sequences=runner.sequences_run,
        other_requests=engine_requests,
        coverage=covered,
        uncovered=sorted(all_keys - set(covered)),
        faults=summary,
        violations=runner.violations.summary(),
    )


def run_hybrid(ops: list[Operation], budget_seconds: float = 3600,
               base_url: str = "http://localhost:8080", **kwargs) -> HybridResult:
    """Synchronous entry point, mirroring engine.strategy.run and run_track_a.

    Defaults to sequential mode, so ``budget_seconds`` is the budget *per half*
    and the call takes about twice that in wall-clock time.
    """
    return asyncio.run(run_hybrid_async(ops, budget_seconds, base_url, **kwargs))


def enabled(env=os.environ) -> bool:
    """Is hybrid mode switched on? Default: no."""
    return str(env.get("HYBRID", "")).strip().lower() in ("1", "true", "yes", "on")
