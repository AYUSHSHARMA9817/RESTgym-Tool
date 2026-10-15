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
                f"(trackA steps={self.track_a_steps} other={self.other_requests}) "
                f"unique5xx={self.unique_5xx} violations={self.unique_violations} "
                f"covered={self.operations_covered}/{self.operations_total} "
                f"in {self.elapsed:.0f}s")


async def run_hybrid_async(ops: list[Operation], budget_seconds: float, base_url: str, *,
                           seed: Optional[int] = None,
                           warmup_fraction: float = DEFAULT_WARMUP_FRACTION,
                           workers: int = 6, client: Optional[Any] = None) -> HybridResult:
    own_client = client is None
    if client is None:
        client = ApiClient(base_url, auth=auth_from_env(),
                           timeout=float(os.environ.get("REQUEST_TIMEOUT", "10")))

    warmup = max(0.0, min(budget_seconds, budget_seconds * warmup_fraction))
    remaining = max(0.0, budget_seconds - warmup)

    runner = TrackARunner(ops, base_url, budget_seconds=budget_seconds, seed=seed, client=client)
    strat = Strategy(ops, base_url, budget_seconds=remaining, workers=workers,
                     seed=seed, client=client)

    # One pool and one fault tracker for both engines: Track A's harvested ids
    # become the engine's inputs, and a 5XX seen by either is counted once.
    runner.pool = strat.pool
    runner.faults = strat.faults

    start = time.monotonic()
    try:
        if warmup > 0:
            log.info("hybrid: warm-up %.0fs (Track A alone, all oracles)", warmup)
            runner.budget = warmup
            await runner.run_async()
            log.info("hybrid: warm-up done -- pool=%d entities=%d",
                     strat.pool.size(), runner.rkg.size())

        # The engine is about to start mutating state concurrently, so retire the
        # oracles that reach conclusions by comparing two separate observations.
        dropped = [o.name for o in runner.oracles if o.active]
        runner.oracles = [o for o in runner.oracles if not o.active]
        log.info("hybrid: parallel phase %.0fs; passive oracles only (%s), stood down: %s",
                 remaining, ", ".join(o.name for o in runner.oracles) or "none",
                 ", ".join(dropped) or "none")

        if remaining > 0:
            runner.budget = remaining
            await asyncio.gather(runner.run_async(), strat.run_async())
    finally:
        if own_client:
            await client.close()

    elapsed = time.monotonic() - start
    return _merge(runner, strat, ops, elapsed)


def _merge(runner: TrackARunner, strat: Strategy, ops: list[Operation],
           elapsed: float) -> HybridResult:
    """Union the two engines' coverage; faults are already shared, so not merged."""
    engine_covered = {k for k, s in strat.stats.items() if s.covered}
    covered = sorted(engine_covered | set(runner.scheduler.covered))
    all_keys = {o.key for o in ops}
    total_requests = getattr(strat._client, "total_requests", 0)
    return HybridResult(
        unique_5xx=strat.faults.unique,
        unique_violations=runner.violations.unique,
        operations_covered=len(covered),
        operations_total=len(ops),
        total_requests=total_requests,
        elapsed=elapsed,
        track_a_steps=runner.steps_sent,
        track_a_sequences=runner.sequences_run,
        other_requests=max(0, total_requests - runner.steps_sent),
        coverage=covered,
        uncovered=sorted(all_keys - set(covered)),
        faults=strat.faults.summary(),
        violations=runner.violations.summary(),
    )


def run_hybrid(ops: list[Operation], budget_seconds: float = 3600,
               base_url: str = "http://localhost:8080", **kwargs) -> HybridResult:
    """Synchronous entry point, mirroring engine.strategy.run and run_track_a."""
    return asyncio.run(run_hybrid_async(ops, budget_seconds, base_url, **kwargs))


def enabled(env=os.environ) -> bool:
    """Is hybrid mode switched on? Default: no."""
    return str(env.get("HYBRID", "")).strip().lower() in ("1", "true", "yes", "on")
