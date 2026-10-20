"""Check the two false-positive defences: value-normalisation tolerance and
the reproduction pass.

They cover different failure modes. Normalisation handles findings that are
wrong every time (a server re-spelling a timestamp); reproduction handles
findings that were a one-off coincidence. Neither subsumes the other.

Run: python3 -m tests.check_soundness
"""
import asyncio

from restleague.models import (Operation, Parameter, Request, RequestBody, Response,
                               Sequence, Step)
from restleague.oracles import CrudRoundtripOracle, OracleContext
from restleague.oracles.base import values_equivalent
from restleague.track_a import TrackARunner
from tests.check_track_a import OPS, BuggyApi

# ------------------------------------------------- normalisation tolerance
# The exact finding a live gestao-hospital produced. Same instant, two
# ISO-8601 spellings; the API was behaving correctly.
assert values_equivalent("2026-10-15T16:02:41Z", "2026-10-15T16:02:41.000+0000")
assert values_equivalent("2026-10-15T16:02:41+00:00", "2026-10-15T16:02:41Z")
assert values_equivalent(5, "5") and values_equivalent(5, "5.0")
assert values_equivalent(True, "true") and values_equivalent(False, "false")
assert values_equivalent(" Leo ", "Leo")
print("OK   server re-encoding (dates, numbers, booleans, padding) is tolerated")

# Real changes must still be caught, including case folding: a server that
# silently upper-cases a name has changed the data.
assert not values_equivalent("Leo", "LEO")
assert not values_equivalent("Leo", "Max")
assert not values_equivalent(True, False)
assert not values_equivalent("2026-10-15T16:00:00Z", "2026-10-15T17:00:00Z")
assert not values_equivalent(5, 6)
print("OK   genuine value changes are still reported")

# end to end through the oracle: the timestamp case must now stay silent
put = Operation("PUT", "/patients/{patientId}",
                parameters=[Parameter("patientId", "path", True, {"type": "integer"})],
                request_body=RequestBody(schema={"properties": {"birthDate": {}, "name": {}}}))
get = Operation("GET", "/patients/{patientId}",
                parameters=[Parameter("patientId", "path", True, {"type": "integer"})])
OPS_ID = {o.key: o for o in (put, get)}


class One:
    def __init__(self, resp):
        self.resp = resp
        self.sent = []

    async def send(self, req):
        self.sent.append(req)
        return self.resp


def check(body, read_back):
    req = Request("PUT", "/patients/{patientId}", path_params={"patientId": 1},
                  body=body, op_key=put.key)
    seq = Sequence()
    seq.append(Step(req, Response(status=200, json={"id": 1})))
    ctx = OracleContext(ops=OPS_ID, client=One(Response(status=200, json=read_back)))
    return asyncio.run(CrudRoundtripOracle().check(seq, ctx))


assert check({"birthDate": "2026-10-15T16:02:41Z"},
             {"id": 1, "birthDate": "2026-10-15T16:02:41.000+0000"}) == [], \
    "the live false positive must not fire any more"
print("OK   the gestao-hospital timestamp false positive no longer fires")

v = check({"name": "Leo"}, {"id": 1, "name": "Max"})
assert v and "not persisted" in v[0].summary, v
print("OK   a field genuinely stored with a different value still fires")

# On an item route the trailing path parameter identifies the entity, even when
# the response body echoes a different id. Reading the body's id instead sent
# the check to another entity, whose unrelated values were then reported as
# "not persisted" (seen live as "name: sent 'Hiro', read back 'beta302'").
client = One(Response(status=200, json={"id": 1, "name": "Leo"}))
req = Request("PUT", "/patients/{patientId}", path_params={"patientId": 1},
              body={"name": "Leo"}, op_key=put.key)
seq = Sequence()
seq.append(Step(req, Response(status=200, json={"id": 99, "name": "Leo"})))
assert asyncio.run(CrudRoundtripOracle().check(
    seq, OracleContext(ops=OPS_ID, client=client))) == []
assert client.sent and client.sent[0].path_params["patientId"] == 1, \
    f"must read back entity 1 from the path, not 99 from the body: {client.sent[0].path_params}"
print("OK   read-back uses the path id, not a different id echoed in the body")

# ...but only on an item route. A POST to a nested *collection* ends in a
# literal segment, so its trailing path parameter is the PARENT; the new
# entity's id is the one the server returns.
from restleague.oracles.crud import _entity_id
create_nested = Request("POST", "/owners/{ownerId}/pets", path_params={"ownerId": 7},
                        body={"name": "Leo"})
assert _entity_id(Response(status=201, json={"id": 42, "name": "Leo"}),
                  create_nested) == 42, "a nested create must use the created id, not the parent's"
item = Request("PUT", "/owners/{ownerId}", path_params={"ownerId": 7}, body={"name": "x"})
assert _entity_id(Response(status=200, json={"id": 99}), item) == 7
print("OK   nested collection creates use the created id, item routes use the path")


# ----------------------------------------------------- reproduction pass
class Flaky(BuggyApi):
    """Reports a field as missing exactly once, then behaves correctly."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    async def send(self, req):
        if req.method == "GET" and req.path == "/owners/{ownerId}":
            self.reads += 1
            i = req.path_params.get("ownerId")
            row = dict(self.owners.get(i, {"id": i}))
            if self.reads == 1:
                row.pop("firstName", None)      # a single unlucky read
            return Response(status=200, json=row)
        return await super().send(req)


async def reproduction():
    # with verification on, a one-off difference is dropped rather than reported
    r = TrackARunner(OPS, "http://fake", budget_seconds=1.0, seed=4,
                     client=Flaky(), workers=1)
    res = await r.run_async()
    print(f"OK   verification on : violations={res.unique_violations} "
          f"dropped-as-unreproduced={res.unreproduced}")
    assert res.unreproduced >= 0      # counter is wired through to the result

    # the mechanism itself: a finding only survives if the re-check agrees
    from restleague.oracles.base import Oracle
    from restleague.models import OracleViolation

    class Once(Oracle):
        name, active = "once", False

        def __init__(self):
            self.calls = 0

        async def check(self, seq, ctx):
            self.calls += 1
            return [OracleViolation(oracle="once", op_key="GET /x",
                                    summary="only on the first call")] if self.calls == 1 else []

    class Always(Oracle):
        name, active = "always", False

        async def check(self, seq, ctx):
            return [OracleViolation(oracle="always", op_key="GET /x", summary="every time")]

    runner = TrackARunner(OPS, "http://fake", client=BuggyApi(), workers=1)
    ctx = OracleContext(ops=runner.ops_by_key, client=None)
    one = Sequence()
    one.append(Step(Request("GET", "/x", op_key="GET /x"), Response(status=200, json={})))

    once = Once()
    kept = await runner._reproduce(once, one, ctx, await once.check(one, ctx))
    assert kept == [], "a finding that does not fire again must be dropped"

    always = Always()
    kept = await runner._reproduce(always, one, ctx, await always.check(one, ctx))
    assert len(kept) == 1 and kept[0].evidence.get("reproduced") is True, kept
    print("OK   one-off findings are dropped; reproducible ones are kept and marked")


asyncio.run(reproduction())
print("ALL CHECKS PASSED")
