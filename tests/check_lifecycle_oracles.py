"""Check the race-condition and dangling-reference oracles.

Run: python3 -m tests.check_lifecycle_oracles
"""
import asyncio

from restleague.models import Operation, Parameter, Request, Response, Sequence, Step
from restleague.oracles import DanglingReferenceOracle, OracleContext, RaceConditionOracle
from restleague.state.graph import RelationalKnowledgeGraph


def race_pair(method, path, s1, s2, op_key):
    """The two steps double_action produces: a tagged pair, twin carries s1."""
    seq = Sequence(target_op_key=op_key)
    r1 = Request(method, path, path_params={"id": 1}, op_key=op_key, mutation="double_action")
    r2 = Request(method, path, path_params={"id": 1}, op_key=op_key, mutation="double_action")
    seq.append(Step(r1, Response(status=s1)))
    twin = Step(r2, Response(status=s2))
    twin.bindings["_twin_status"] = s1
    seq.append(twin)
    return seq


def run(oracle, seq, **ctx):
    return asyncio.run(oracle.check(seq, OracleContext(**ctx)))


# --------------------------------------------------------- race (D7)
# two concurrent DELETEs both 204 -> the second should have 404'd
v = run(RaceConditionOracle(), race_pair("DELETE", "/items/{id}", 204, 204, "DELETE /items/{id}"))
assert v and v[0].severity == "violation" and "both succeeded" in v[0].summary, v
print("OK   race flags two concurrent DELETEs both succeeding")

# one 204, one 404 -> correctly serialised, silent
assert run(RaceConditionOracle(), race_pair("DELETE", "/items/{id}", 204, 404, "DELETE /items/{id}")) == []
print("OK   race stays silent when one of the two is rejected")

# two concurrent item PUTs both ok -> silent: PUT is idempotent, both ok is correct
assert run(RaceConditionOracle(), race_pair("PUT", "/items/{id}", 200, 200, "PUT /items/{id}")) == []
print("OK   race ignores two concurrent PUTs (idempotent, both ok is correct)")

# two concurrent collection POSTs both created -> warning (possible duplicate)
v = run(RaceConditionOracle(), race_pair("POST", "/items", 201, 201, "POST /items"))
assert v and v[0].severity == "warning" and "two resources" in v[0].summary, v
print("OK   race warns on two concurrent creates both succeeding")

# a normal (non-double_action) step is ignored entirely
plain = Sequence()
plain.append(Step(Request("DELETE", "/items/{id}", path_params={"id": 1}, op_key="DELETE /items/{id}"),
                  Response(status=204)))
assert run(RaceConditionOracle(), plain) == []
print("OK   race ignores steps that were not a double-action pair")


# ------------------------------------------------ dangling reference (D6)
owner_del = Operation("DELETE", "/owners/{ownerId}",
                      parameters=[Parameter("ownerId", "path", True, {"type": "integer"})])
pet_get = Operation("GET", "/pets/{petId}",            # standalone child route
                    parameters=[Parameter("petId", "path", True, {"type": "integer"})])
OPS = {o.key: o for o in (owner_del, pet_get)}


class Client:
    def __init__(self, resp):
        self.resp, self.sent = resp, []

    async def send(self, req):
        self.sent.append(req)
        return self.resp


def deleted_owner_with_pet():
    g = RelationalKnowledgeGraph(seed=1)
    g.add_entity("owner", 1)
    g.add_entity("pet", 10, parent=("owner", 1))
    g.cascade_delete("owner", 1)                        # marks owner 1 and pet 10 deleted
    seq = Sequence(target_op_key=owner_del.key)
    seq.append(Step(Request("DELETE", "/owners/{ownerId}", path_params={"ownerId": 1},
                            op_key=owner_del.key), Response(status=204)))
    return g, seq


# the orphaned pet is still readable -> dangling reference
g, seq = deleted_owner_with_pet()
client = Client(Response(status=200, json={"id": 10}))
v = run(DanglingReferenceOracle(), seq, ops=OPS, client=client, rkg=g)
assert v and v[0].severity == "violation" and "still readable" in v[0].summary, v
assert client.sent and client.sent[0].path_params == {"petId": 10}, client.sent
print("OK   dangling flags a child still readable after its parent's delete")

# the orphaned pet is correctly gone (404) -> silent
g, seq = deleted_owner_with_pet()
assert run(DanglingReferenceOracle(), seq, ops=OPS, client=Client(Response(status=404)), rkg=g) == []
print("OK   dangling stays silent when the child is correctly gone")

# child only reachable under the (now-deleted) parent -> inconclusive, skipped
nested_get = Operation("GET", "/owners/{ownerId}/pets/{petId}",
                       parameters=[Parameter("ownerId", "path", True, {}),
                                   Parameter("petId", "path", True, {})])
g, seq = deleted_owner_with_pet()
nested_client = Client(Response(status=200, json={"id": 10}))
assert run(DanglingReferenceOracle(), seq, ops={owner_del.key: owner_del, nested_get.key: nested_get},
           client=nested_client, rkg=g) == []
assert nested_client.sent == [], "a child with no standalone route must not be probed"
print("OK   dangling skips a child with no standalone route (would 404 for the right reason)")

print("ALL CHECKS PASSED")

# a concurrent login is not a duplicate-create: both succeeding is expected
assert run(RaceConditionOracle(), race_pair("POST", "/users/v1/login", 200, 200, "POST /users/v1/login")) == []
print("OK   race ignores concurrent logins (auth action, not a create)")
