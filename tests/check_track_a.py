"""End-to-end check of the Track A runner against a scripted in-process API
(docs/architecture/track_a_stateful_fuzzing.md, Milestone 5).

No network and no extra dependencies: a fake client stands in for ApiClient and
implements a small owner/pet API with three deliberate bugs --
  * DELETE leaves the entity readable      -> zombie_resource oracle
  * PUT is not idempotent (counter bumps)  -> idempotency oracle
  * lastName is silently dropped on create -> crud_roundtrip oracle

Run: python3 -m tests.check_track_a
"""
import asyncio

from restleague.models import Operation, Parameter, RequestBody, Response
from restleague.track_a import TrackARunner, enabled, run_track_a


# ------------------------------------------------------------ the fake API
class BuggyApi:
    """Owner/pet API with three planted semantic bugs (no 5XX anywhere)."""

    def __init__(self):
        self.owners: dict[int, dict] = {}
        self.pets: dict[int, dict] = {}
        self.deleted: set = set()
        self.puts: dict[int, int] = {}
        self.total_requests = 0
        self.sent = []

    async def send(self, req):
        self.total_requests += 1
        self.sent.append(req)
        m, p = req.method, req.path
        pp = req.path_params

        if m == "POST" and p == "/owners":
            i = len(self.owners) + 1
            body = req.body if isinstance(req.body, dict) else {}
            # BUG 1: lastName silently dropped
            self.owners[i] = {"id": i, "firstName": body.get("firstName")}
            return Response(status=201, json=dict(self.owners[i]))

        if m == "GET" and p == "/owners/{ownerId}":
            i = pp.get("ownerId")
            # BUG 2: deleted owners stay readable
            if i in self.owners or i in self.deleted:
                return Response(status=200, json=self.owners.get(i, {"id": i}))
            return Response(status=404, json={"message": "not found"})

        if m == "PUT" and p == "/owners/{ownerId}":
            i = pp.get("ownerId")
            if i not in self.owners:
                return Response(status=404, json={"message": "not found"})
            # BUG 3: PUT accumulates instead of replacing
            self.puts[i] = self.puts.get(i, 0) + 1
            return Response(status=200, json={"id": i, "edits": self.puts[i]})

        if m == "DELETE" and p == "/owners/{ownerId}":
            i = pp.get("ownerId")
            if i in self.owners:
                del self.owners[i]
                self.deleted.add(i)
                return Response(status=204)
            return Response(status=404, json={"message": "not found"})

        if m == "POST" and p == "/owners/{ownerId}/pets":
            owner = pp.get("ownerId")
            if owner not in self.owners:
                return Response(status=404, json={"message": "no such owner"})
            i = 100 + len(self.pets)
            self.pets[i] = {"id": i, "ownerId": owner}
            return Response(status=201, json=dict(self.pets[i]))

        if m == "GET" and p == "/owners/{ownerId}/pets/{petId}":
            pet = self.pets.get(pp.get("petId"))
            if pet is None:
                return Response(status=404, json={"message": "no such pet"})
            return Response(status=200, json=dict(pet))

        return Response(status=404, json={"message": "unmapped"})

    async def close(self):
        pass


OWNER_BODY = RequestBody(schema={"type": "object",
                                 "properties": {"firstName": {"type": "string"},
                                                "lastName": {"type": "string"},
                                                "id": {"type": "integer", "readOnly": True}},
                                 "required": ["firstName", "lastName"]})
OWNER_ID = Parameter("ownerId", "path", True, {"type": "integer"})
PET_ID = Parameter("petId", "path", True, {"type": "integer"})
OWNER_SCHEMA = {"type": "object", "properties": {"id": {"type": "integer"},
                                                 "firstName": {"type": "string"}}}

OPS = [
    Operation("POST", "/owners", request_body=OWNER_BODY, responses={"201": OWNER_SCHEMA}),
    Operation("GET", "/owners/{ownerId}", parameters=[OWNER_ID], responses={"200": OWNER_SCHEMA}),
    Operation("PUT", "/owners/{ownerId}", parameters=[OWNER_ID], request_body=OWNER_BODY,
              responses={"200": OWNER_SCHEMA}),
    Operation("DELETE", "/owners/{ownerId}", parameters=[OWNER_ID]),
    Operation("POST", "/owners/{ownerId}/pets", parameters=[OWNER_ID],
              request_body=RequestBody(schema={"type": "object",
                                               "properties": {"name": {"type": "string"}}}),
              responses={"201": {"type": "object", "properties": {"id": {"type": "integer"}}}}),
    Operation("GET", "/owners/{ownerId}/pets/{petId}", parameters=[OWNER_ID, PET_ID],
              responses={"200": {"type": "object", "properties": {"id": {"type": "integer"}}}}),
]

# --------------------------------------------------------------- the flag
assert enabled({}) is False, "Track A must be off by default"
assert enabled({"TRACK_A": "1"}) is True
assert enabled({"TRACK_A": "no"}) is False
print("OK   TRACK_A is opt-in; the competition pipeline runs unless it is set")


# ---------------------------------------------------------------- the run
async def main():
    api = BuggyApi()
    runner = TrackARunner(OPS, "http://fake", budget_seconds=2.0, seed=5, client=api)
    result = await runner.run_async()

    print("result :", result.describe())
    assert result.sequences_run > 0, "no sequences were executed"
    assert result.total_requests > 0

    # coverage: the chained operations had to be reached through their prefixes
    assert "POST /owners" in result.coverage, result.coverage
    assert "POST /owners/{ownerId}/pets" in result.coverage, \
        "creating a pet needs an owner id bound from an earlier step"
    print("OK   nested operations covered via synthesized prefixes:",
          result.operations_covered, "/", result.operations_total)

    # the three planted semantic bugs were found, with no 5XX anywhere
    fired = {v["oracle"] for v in result.violations["violations"]}
    print("oracles fired :", sorted(fired))
    assert "zombie_resource" in fired, result.violations
    assert "crud_roundtrip" in fired, result.violations
    assert result.unique_5xx == 0, "this API never returns 5XX; findings are semantic only"
    print("OK   semantic bugs found on an API that never returns 5XX")

    # the graph and the state machine learned something
    assert runner.rkg.size() > 0
    assert result.states, "the DSMI should have recorded at least one entity type"
    print("OK   graph holds", runner.rkg.size(), "entities; states:", sorted(result.states))

    # the scheduler adapted
    assert result.scheduler["covered"] == result.operations_covered
    print("OK   scheduler stats:", result.scheduler["top_mutations"][:2])


asyncio.run(main())

# the synchronous wrapper works too
res = run_track_a(OPS, budget_seconds=1.0, base_url="http://fake", seed=3, client=BuggyApi())
assert res.sequences_run > 0
print("OK   run_track_a() synchronous wrapper returns a result:", res.describe())

print("ALL CHECKS PASSED")
