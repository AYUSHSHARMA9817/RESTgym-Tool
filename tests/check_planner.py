"""Checks for the optional LLM prerequisite planner (restleague/sequence/planner.py).

No network and no `anthropic` dependency: a fake backend stands in for the model,
so these run anywhere. They prove the three properties the design promises:

  * OFF BY DEFAULT -- without LLM_PLANNER the planner is a NullPlanner and the
    executor behaves exactly as the spec-only baseline (the "without AI" arm).
  * DETERMINISTIC / OFFLINE -- a solved hint set is cached to disk and replayed
    with no further backend calls (the "with AI, reproducible" arm).
  * SOUND BY CONSTRUCTION -- hints only reach VALID requests and only change
    values sent; nothing here touches oracle judgement.

Run: python3 -m tests.check_planner
"""
import os
import tempfile

from restleague.models import Mode, Operation, RequestBody, Request
from restleague.sequence.executor import AsyncSequenceExecutor
from restleague.sequence.planner import (CachingPlanner, NullPlanner, enabled,
                                         make_planner, op_signature)

BODY = RequestBody(schema={"type": "object",
                           "properties": {"status": {"type": "string"},
                                          "quantity": {"type": "integer"}},
                           "required": ["status", "quantity"]})
OP = Operation("PUT", "/orders/{id}", operation_id="update_order", request_body=BODY,
               responses={"200": {}})


# ---------------------------------------------------------- off by default
assert enabled({}) is False, "planner must be off unless LLM_PLANNER is set"
assert enabled({"LLM_PLANNER": "1"}) is True
assert enabled({"LLM_PLANNER": "no"}) is False
assert isinstance(make_planner({}, api="crapi"), NullPlanner), \
    "no LLM_PLANNER -> NullPlanner (no AI, no dependency)"
assert make_planner({}).hints(OP) == {}, "NullPlanner suggests nothing"
print("OK   off by default: make_planner() is a NullPlanner, hints are empty")


# ------------------------------------------- caching + offline determinism
class FakeBackend:
    """Stands in for the Anthropic backend; counts how often it is consulted."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def propose(self, op):
        self.calls += 1
        return dict(self.reply)


tmp = tempfile.mkdtemp()
cache = os.path.join(tmp, "crapi.planner-cache.json")
backend = FakeBackend({"status": "delivered"})
planner = CachingPlanner(backend, cache_path=cache)

h1 = planner.hints(OP)
h2 = planner.hints(OP)
assert h1 == {"status": "delivered"}, h1
assert backend.calls == 1, "a cache hit must not call the backend again"
assert os.path.exists(cache), "the hint set must be persisted to disk"
print("OK   backend consulted once per operation; result cached in memory")

# a fresh planner over the same file, with a backend that would raise if used,
# reproduces the hints with no backend call -- i.e. the run replays offline
class ExplodingBackend:
    def propose(self, op):
        raise AssertionError("offline replay must not touch the backend")

replay = CachingPlanner(ExplodingBackend(), cache_path=cache)
assert replay.hints(OP) == {"status": "delivered"}, "committed cache must replay offline"
print("OK   committed cache replays deterministically with the model offline")

# an empty proposal is cached too, so a barren op is not re-queried every window
barren = FakeBackend({})
bp = CachingPlanner(barren, cache_path=os.path.join(tmp, "b.json"))
bp.hints(OP); bp.hints(OP)
assert barren.calls == 1, "empty proposals must also be cached"
print("OK   empty proposals are cached (no repeated calls for barren ops)")

assert op_signature(OP) == op_signature(OP), "signature must be stable"
OP2 = Operation("PUT", "/orders/{id}", operation_id="update_order",
                request_body=RequestBody(schema={"type": "object",
                                                 "properties": {"x": {"type": "string"}}}),
                responses={"200": {}})
assert op_signature(OP) != op_signature(OP2), "a changed schema must re-plan"
print("OK   op_signature is stable and schema-sensitive")


# --------------------------------------- executor applies hints (VALID only)
class StubPlanner:
    def hints(self, op):
        return {"status": "delivered"}


class FakeRKG:
    def query_path_bindings(self, path, op):
        return {}
    def query_cross_entity_bindings(self, path):
        return None


def gen(op, pool, mode):
    # a schema-only generator that puts a junk status the server would reject
    return Request(method=op.method, path=op.path,
                   body={"status": "zzz_junk", "quantity": 2})


ex = AsyncSequenceExecutor(client=None, generate_request=gen, planner=StubPlanner())
valid = ex.build_request(OP, FakeRKG(), Mode.VALID)
assert valid.body["status"] == "delivered", "hint must override the VALID body value"
assert valid.body["quantity"] == 2, "un-hinted fields are left as generated"
print("OK   hints override values on a VALID request, leaving others intact")

invalid = ex.build_request(OP, FakeRKG(), Mode.INVALID)
assert invalid.body["status"] == "zzz_junk", "hints must NOT touch non-VALID requests"
print("OK   hints are not applied to INVALID requests (soundness scope)")

# and with no planner at all, generation is untouched (the baseline)
base = AsyncSequenceExecutor(client=None, generate_request=gen, planner=None)
b = base.build_request(OP, FakeRKG(), Mode.VALID)
assert b.body["status"] == "zzz_junk", "no planner -> spec-only values, unchanged"
print("OK   with no planner the request is the exact spec-only baseline")

print("ALL CHECKS PASSED")
