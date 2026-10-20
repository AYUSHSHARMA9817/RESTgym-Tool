"""Check SequenceSynthesizer plans prefixes and AsyncSequenceExecutor binds ids
across steps (docs/architecture/track_a_stateful_fuzzing.md, Milestone 2).

Run: python3 -m tests.check_sequence
"""
import asyncio

from restleague.models import Mode, Response
from restleague.sequence.executor import AsyncSequenceExecutor, harvest_to_graph
from restleague.sequence.synthesizer import SequenceSynthesizer, type_of_resource_key
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec
from restleague.spec.values import generate_request
from restleague.state.graph import RelationalKnowledgeGraph

assert type_of_resource_key("ownerid") == "owner"
assert type_of_resource_key("pet") == "pet"

ops = order_operations(parse_spec("specs/pet-clinic-openapi.json"))
by_key = {o.key: o for o in ops}
synth = SequenceSynthesizer(ops)

# ---------------------------------------------------------------- planning
target = by_key["POST /petclinic/api/owners/{ownerId}/pets"]
plan = synth.synthesize(target, rkg=None)          # cold start: nothing exists yet
keys = [o.key for o in plan]
print("plan (cold) :", keys)
assert plan[-1].key == target.key, keys
assert synth.is_sequence(plan), "creating a pet under an owner needs >= 2 steps"
assert any("POST /petclinic/api/owners" == k for k in keys[:-1]), keys
assert len(plan) <= 8, "doc caps sequences at 8 steps"

# A deeper target pulls in a longer prefix.
deep = by_key["POST /petclinic/api/owners/{ownerId}/pets/{petId}/visits"]
deep_plan = synth.synthesize(deep, rkg=None)
print("plan (deep) :", [o.key for o in deep_plan])
assert deep_plan[-1].key == deep.key
assert len(deep_plan) >= 2, deep_plan

# With the prerequisite already live in the graph, no producer step is added.
warm = RelationalKnowledgeGraph(seed=1)
warm.add_entity("owner", 1, {"id": 1})
warm_plan = synth.synthesize(target, rkg=warm)
print("plan (warm) :", [o.key for o in warm_plan])
assert len(warm_plan) < len(plan), "a live owner should remove the POST /owners step"
assert warm_plan[-1].key == target.key
print("OK   synthesizer prepends producers only for unsatisfied prerequisites")

# ------------------------------------------------------------- harvesting
g = RelationalKnowledgeGraph(seed=1)
owner_op = by_key["POST /petclinic/api/owners"]
req = generate_request(owner_op, None, Mode.VALID)
harvest_to_graph(g, owner_op, req, Response(status=201, json={"id": 1, "firstName": "George"}))
assert g.get_entity("owner", 1) is not None, g.snapshot()

pet_op = by_key["POST /petclinic/api/owners/{ownerId}/pets"]
pet_req = generate_request(pet_op, None, Mode.VALID)
pet_req.path_params["ownerId"] = 1
harvest_to_graph(g, pet_op, pet_req, Response(status=201, json={"id": 10, "name": "Leo"}))
assert [n.entity_id for n in g.children_of("owner", 1)] == [10], g.snapshot()
print("OK   harvest_to_graph links a created child to the parent in its path")

# A DELETE cascades through the graph.
del_op = by_key["DELETE /petclinic/api/owners/{ownerId}"]
del_req = generate_request(del_op, None, Mode.VALID)
del_req.path_params["ownerId"] = 1
harvest_to_graph(g, del_op, del_req, Response(status=204))
assert g.get_entity("owner", 1).deleted and g.get_entity("pet", 10).deleted, g.snapshot()
print("OK   a successful DELETE cascades to children in the graph")

# -------------------------------------------------------------- execution
class FakeClient:
    """Stands in for engine.client.ApiClient: records requests, returns created ids."""

    def __init__(self):
        self.sent = []
        self._next_id = {"owner": 1, "pet": 100}

    async def send(self, req):
        self.sent.append(req)
        if req.method == "POST" and req.path.endswith("/owners"):
            i = self._next_id["owner"]
            self._next_id["owner"] += 1
            return Response(status=201, json={"id": i, "firstName": "George"})
        if req.method == "POST" and req.path.endswith("/pets"):
            i = self._next_id["pet"]
            self._next_id["pet"] += 1
            return Response(status=201, json={"id": i, "name": "Leo"})
        return Response(status=200, json={})


async def main():
    g = RelationalKnowledgeGraph(seed=1)
    client = FakeClient()
    ex = AsyncSequenceExecutor(client, generate_request, pool=None)
    plan = synth.synthesize(target, rkg=g)
    seq = await ex.execute(plan, g)

    print("executed    :", [s.request.op_key for s in seq.steps])
    assert len(seq) == len(plan), (len(seq), len(plan))
    assert all(s.response is not None and s.response.ok for s in seq.steps), "all steps should succeed"

    # the pet step must have been bound to the owner id created by an earlier step
    owner_step = next(s for s in seq.steps if s.request.op_key == "POST /petclinic/api/owners")
    owner_id = owner_step.response.json["id"]
    pet_step = seq.steps[-1]
    assert pet_step.bindings.get("ownerId") == owner_id, (pet_step.bindings, owner_id)
    print("OK   executor passed ownerId=%r from step 1 into step 2" % owner_id)

    # and the resulting graph records the parent/child link
    pet_id = pet_step.response.json["id"]
    assert [n.entity_id for n in g.children_of("owner", owner_id)] == [pet_id], g.snapshot()

    # a failing prefix step abandons the rest of the chain
    class DeadClient(FakeClient):
        async def send(self, req):
            self.sent.append(req)
            return Response(status=500, text="boom")

    g2 = RelationalKnowledgeGraph(seed=1)
    ex2 = AsyncSequenceExecutor(DeadClient(), generate_request, pool=None)
    seq2 = await ex2.execute(synth.synthesize(target, rkg=g2), g2)
    assert len(seq2) == 1, "should stop after the first failed prefix step"
    print("OK   a failed prefix step abandons the remaining steps")


asyncio.run(main())
print("ALL CHECKS PASSED")
