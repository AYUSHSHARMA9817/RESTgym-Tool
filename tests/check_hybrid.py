"""Check hybrid mode: shared state, oracle gating, and merged results.

Run: python3 -m tests.check_hybrid
"""
import asyncio

from restleague.hybrid import enabled, run_hybrid_async
from restleague.track_a import TrackARunner
from restleague.engine.strategy import Strategy
from tests.check_track_a import OPS, BuggyApi


class HybridApi(BuggyApi):
    """BuggyApi plus the rest of the ApiClient protocol that Strategy uses.

    Track A only ever calls send(); the default engine additionally probes for
    liveness and tracks transport errors, so a fake shared by both needs the
    full surface.
    """
    total_transport_errors = 0
    api_down = False

    async def probe(self) -> bool:
        return True

    async def wait_until_alive(self, deadline) -> bool:
        return True

# ------------------------------------------------------------- the flag
assert enabled({}) is False, "hybrid must be opt-in"
assert enabled({"HYBRID": "1"}) is True
assert enabled({"HYBRID": "off"}) is False
print("OK   HYBRID is opt-in; the competition pipeline runs unless it is set")


# ------------------------------------------- sharing needs no engine edits
class Dummy:
    total_requests = 0

    async def send(self, req):
        return None

    async def close(self):
        pass


strat = Strategy([], "http://x", client=Dummy())
runner = TrackARunner(OPS, "http://x", client=Dummy())
runner.pool = strat.pool
runner.faults = strat.faults
assert runner.pool is strat.pool and runner.faults is strat.faults
print("OK   pool and fault tracker can be shared without modifying engine/strategy.py")


# ----------------------------------------------------- oracle gating
async def gating():
    api = HybridApi()
    r = TrackARunner(OPS, "http://fake", budget_seconds=0.5, seed=1, client=api)
    all_names = {o.name for o in r.oracles}
    passive = {o.name for o in r.oracles if not o.active}
    active = all_names - passive
    # the parallel-safe set is exactly the oracles that send no extra requests
    # of their own -- they only inspect what the run already did, so concurrent
    # traffic cannot mislead them
    assert passive == {"schema_conformance", "cross_entity_access", "race_condition",
                       "second_order_injection", "reflected_payload", "timing_injection",
                       "cors_misconfig"}, passive
    assert "idempotency" in active and "safety" in active and "crud_roundtrip" in active
    print("OK   passive (parallel-safe) oracles:", sorted(passive))
    print("OK   comparison-based oracles stood down in parallel:", sorted(active))


asyncio.run(gating())


# ------------------------------------------------------- end to end
async def end_to_end():
    api = HybridApi()
    res = await run_hybrid_async(OPS, budget_seconds=2.0, base_url="http://fake",
                                 seed=3, client=api, warmup_fraction=0.3)
    print()
    print(res.describe())

    assert res.total_requests > 0, "nothing was sent"
    assert res.track_a_sequences > 0, "Track A did not run"
    assert res.other_requests > 0, "the default engine did not run"
    assert res.track_a_steps + res.other_requests == res.total_requests
    print("OK   both engines ran and the request accounting adds up")

    # the warm-up phase runs Track A alone with every oracle, so the planted
    # semantic bugs are still found despite the parallel phase that follows
    assert res.unique_violations > 0, "warm-up should still catch the planted bugs"
    print("OK   semantic bugs still found during the quiet warm-up:",
          res.violations["by_oracle"])

    # coverage is the union of what either engine reached
    assert res.operations_covered > 0
    assert res.operations_covered + len(res.uncovered) == res.operations_total
    print(f"OK   merged coverage {res.operations_covered}/{res.operations_total}")


asyncio.run(end_to_end())


# ------------------------------------------- sequential vs parallel modes
async def modes():
    # Sequential is the default: each half gets its own full budget, so the
    # call takes about twice budget_seconds in wall-clock time.
    seq = await run_hybrid_async(OPS, budget_seconds=1.0, base_url="http://fake",
                                 seed=3, client=HybridApi(), mode="sequential")
    par = await run_hybrid_async(OPS, budget_seconds=1.0, base_url="http://fake",
                                 seed=3, client=HybridApi(), mode="parallel",
                                 warmup_fraction=0.3)
    print()
    print("sequential:", seq.describe())
    print("parallel  :", par.describe())
    assert seq.total_requests > 0 and par.total_requests > 0

    # The point of sequential mode: nothing else is mutating the API while
    # Track A runs, so the comparison-based oracles stay valid for its whole
    # phase instead of only a short warm-up.
    solo = TrackARunner(OPS, "http://fake", client=HybridApi(), workers=1)
    assert len(solo.oracles) == 16, [o.name for o in solo.oracles]
    concurrent = TrackARunner(OPS, "http://fake", client=HybridApi(), workers=4)
    assert len(concurrent.oracles) == 7, [o.name for o in concurrent.oracles]
    print(f"OK   sequential keeps all {len(solo.oracles)} oracles; "
          f"concurrent keeps {len(concurrent.oracles)} (the parallel-safe ones)")


asyncio.run(modes())
print("ALL CHECKS PASSED")
