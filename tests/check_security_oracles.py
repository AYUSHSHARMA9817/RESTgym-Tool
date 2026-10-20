"""Check the authorization and mass-assignment oracles, and that attack
payloads reach the final step of a sequence.

Run: python3 -m tests.check_security_oracles
"""
import asyncio

from restleague.models import (Mode, Operation, Parameter, Request, RequestBody, Response,
                               Sequence, Step)
from restleague.oracles import CrossEntityAccessOracle, MassAssignmentOracle, OracleContext
from restleague.sequence.executor import AsyncSequenceExecutor
from restleague.spec.values import generate_request
from restleague.state.graph import RelationalKnowledgeGraph

OWNER_ID = Parameter("ownerId", "path", True, {"type": "integer"})
PET_ID = Parameter("petId", "path", True, {"type": "integer"})
pet_item = Operation("GET", "/owners/{ownerId}/pets/{petId}", parameters=[OWNER_ID, PET_ID])
pet_put = Operation("PUT", "/owners/{ownerId}/pets/{petId}", parameters=[OWNER_ID, PET_ID])
OPS = {o.key: o for o in (pet_item, pet_put)}


def one(req, resp):
    s = Sequence()
    s.append(Step(req, resp))
    return s


def run(oracle, seq, client=None):
    return asyncio.run(oracle.check(seq, OracleContext(ops=OPS, client=client)))


# ------------------------------------------------- cross-entity (BOLA)
# owner 1's URL, but pet 20 belongs to owner 2. The mutation tag is what the
# executor sets when it used query_cross_entity_bindings.
attack = Request("GET", "/owners/{ownerId}/pets/{petId}",
                 path_params={"ownerId": 1, "petId": 20},
                 op_key=pet_item.key, mutation="cross_entity")

# served the foreign record -> highest confidence
v = run(CrossEntityAccessOracle(), one(attack, Response(status=200, json={"id": 20, "name": "Max"})))
assert v and v[0].severity == "violation", v
assert "belongs to a different parent" in v[0].summary, v[0].summary
print("OK   BOLA oracle flags a 200 that returned another owner's pet")

# refused -> correct behaviour, silent
assert run(CrossEntityAccessOracle(), one(attack, Response(status=404))) == []
assert run(CrossEntityAccessOracle(), one(attack, Response(status=403))) == []
print("OK   BOLA oracle stays silent when the API refuses (403/404)")

# a 2XX whose body does not prove what was served -> warning, not violation
v = run(CrossEntityAccessOracle(), one(attack, Response(status=200, json={})))
assert v and v[0].severity == "warning", v
print("OK   BOLA oracle downgrades to a warning when the body proves nothing")

# a write accepted under the wrong parent is a violation even without a body
w = Request("PUT", "/owners/{ownerId}/pets/{petId}", path_params={"ownerId": 1, "petId": 20},
            body={"name": "x"}, op_key=pet_put.key, mutation="cross_entity")
v = run(CrossEntityAccessOracle(), one(w, Response(status=204)))
assert v and v[0].severity == "violation", v
print("OK   BOLA oracle treats an accepted cross-parent WRITE as a violation")

# an ordinary (non-cross-entity) request is never judged by this oracle
normal = Request("GET", "/owners/{ownerId}/pets/{petId}", path_params={"ownerId": 1, "petId": 10},
                 op_key=pet_item.key)
assert run(CrossEntityAccessOracle(), one(normal, Response(status=200, json={"id": 10}))) == []
print("OK   BOLA oracle ignores requests that were not cross-entity probes")

# ----------------------------------------------------- mass assignment
create = Operation("POST", "/owners", request_body=RequestBody(
    schema={"type": "object", "properties": {"firstName": {}, "lastName": {}}}))
item = Operation("GET", "/owners/{ownerId}", parameters=[OWNER_ID])
MA_OPS = {o.key: o for o in (create, item)}


class Server:
    """Configurable server: does it store undeclared fields?"""

    def __init__(self, greedy: bool, echo: bool = True):
        self.greedy, self.echo = greedy, echo
        self.rows = {}
        self.sent = []

    async def send(self, req):
        self.sent.append(req)
        if req.method == "POST":
            body = dict(req.body or {})
            kept = body if self.greedy else {k: v for k, v in body.items()
                                             if k in ("firstName", "lastName")}
            kept["id"] = 1
            self.rows[1] = kept
            return Response(status=201, json=dict(kept) if self.echo else {"id": 1})
        return Response(status=200, json=dict(self.rows.get(req.path_params.get("ownerId"), {})))


def run_ma(server):
    req = Request("POST", "/owners", body={"firstName": "Leo"}, op_key=create.key)
    seq = one(req, Response(status=201, json={"id": 1, "firstName": "Leo"}))
    return asyncio.run(MassAssignmentOracle().check(
        seq, OracleContext(ops=MA_OPS, client=server)))


v = run_ma(Server(greedy=True))
assert v, "a server that stores isAdmin must be flagged"
assert "persisted" in v[0].summary and v[0].severity == "violation", v
print("OK   mass-assignment oracle flags undeclared fields that persist:",
      v[0].evidence["fields"][:3])

assert run_ma(Server(greedy=False)) == [], "a server that drops unknown fields is correct"
print("OK   mass-assignment oracle stays silent when unknown fields are dropped")

# when the write response does not echo, it falls back to the item GET
srv = Server(greedy=True, echo=False)
v = run_ma(srv)
assert v, v
assert any(r.method == "GET" for r in srv.sent), "should have read the entity back"
print("OK   mass-assignment oracle reads the entity back when the write does not echo")


# --------------------------------------------- attack payload placement
class Recorder:
    def __init__(self):
        self.sent = []

    async def send(self, req):
        self.sent.append(req)
        return Response(status=201, json={"id": len(self.sent)})


async def attack_placement():
    owners = Operation("POST", "/owners", request_body=RequestBody(
        schema={"type": "object", "properties": {"firstName": {"type": "string"}}}))
    pets = Operation("POST", "/owners/{ownerId}/pets", parameters=[OWNER_ID],
                     request_body=RequestBody(schema={"type": "object",
                                                      "properties": {"name": {"type": "string"}}}))
    rec = Recorder()
    ex = AsyncSequenceExecutor(rec, generate_request, pool=None, seed=3)
    g = RelationalKnowledgeGraph(seed=1)
    seq = await ex.execute([owners, pets], g, attack="injection")

    assert len(seq.steps) == 2, seq.steps
    setup, target = seq.steps[0].request, seq.steps[-1].request
    assert setup.mutation is None, f"setup step must stay clean, got {setup.mutation!r}"
    assert target.mutation is not None, "the final step should carry the attack"
    print("OK   attack payload lands on the final step only (setup stayed clean)")
    print("     final-step mutation tag:", target.mutation)

    # the ids that made the endpoint reachable survive the mutation
    assert "ownerId" in target.path_params, target.path_params
    print("OK   path ids survive the payload mutation, so the endpoint stays reachable")


asyncio.run(attack_placement())
print("ALL CHECKS PASSED")
