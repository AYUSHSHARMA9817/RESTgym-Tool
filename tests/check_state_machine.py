"""Check the state-machine inferrer and the sequence mutator
(docs/architecture/track_a_stateful_fuzzing.md, Milestone 4).

Run: python3 -m tests.check_state_machine
"""
import asyncio

from restleague.models import Operation, Request, Response
from restleague.sequence.executor import AsyncSequenceExecutor
from restleague.sequence.mutator import OPERATORS, SequenceMutator
from restleague.sequence.synthesizer import SequenceSynthesizer
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec
from restleague.spec.values import generate_request
from restleague.state.graph import RelationalKnowledgeGraph
from restleague.state.inferrer import DynamicStateMachineInferrer, explicit_state

# ------------------------------------------------------- state signatures
assert explicit_state({"id": 1, "status": "PAID"}) == "PAID"
assert explicit_state({"order": {"state": "shipped"}}) == "SHIPPED"
assert explicit_state({"id": 1, "name": "x"}) is None
print("OK   explicit_state reads a lifecycle field, nested one level if needed")

order_post = Operation("POST", "/orders")
order_pay = Operation("PUT", "/orders/{orderId}/pay")
order_del = Operation("DELETE", "/orders/{orderId}")

dsmi = DynamicStateMachineInferrer()
req = Request("POST", "/orders")
dsmi.observe(order_post, req, Response(status=201, json={"id": 1, "status": "CREATED"}),
             entity=("order", 1))
assert dsmi.state_of("order", 1) == "CREATED"

pay = Request("PUT", "/orders/{orderId}/pay", path_params={"orderId": 1})
dsmi.observe(order_pay, pay, Response(status=200, json={"id": 1, "status": "PAID"}),
             entity=("order", 1))
assert dsmi.state_of("order", 1) == "PAID"
assert dsmi.states_of("order") == {"PAID"}, dsmi.states_of("order")

trans = dsmi.transitions("order")
assert ("CREATED", order_pay.key) in trans, trans
assert trans[("CREATED", order_pay.key)] == {"PAID": 1}, trans
print("OK   DSMI records CREATED --PUT /orders/{id}/pay--> PAID")

assert order_pay.key in dsmi.seen_operations("order", "CREATED")
unseen = dsmi.unseen_operations("order", "PAID", [order_pay, order_del])
assert {o.key for o in unseen} == {order_pay.key, order_del.key}, unseen
print("OK   DSMI reports operations never observed from a state (illegal-transition probes)")

# Capability-mask fallback for APIs with no status field at all. The mask
# reports capabilities *lost*, so merely probing a new method is not a state
# change -- otherwise the inferrer would invent transitions out of our own
# probing order.
d2 = DynamicStateMachineInferrer()
d2.observe(Operation("GET", "/carts/{id}"), Request("GET", "/carts/{id}"),
           Response(status=200, json={"id": 9}), entity=("cart", 9))
assert d2.state_of("cart", 9) == "CAP:open", d2.state_of("cart", 9)
d2.observe(Operation("PUT", "/carts/{id}"), Request("PUT", "/carts/{id}"),
           Response(status=200, json={"id": 9}), entity=("cart", 9))
assert d2.state_of("cart", 9) == "CAP:open", "probing a new accepted method is not a state change"
d2.observe(Operation("PUT", "/carts/{id}"), Request("PUT", "/carts/{id}"),
           Response(status=409, json={"id": 9}), entity=("cart", 9))
assert d2.state_of("cart", 9) == "CAP:-PUT", d2.state_of("cart", 9)
# losing a capability IS a state change, and must be recorded as a transition
assert "CAP:-PUT" in d2.transitions("cart")[("CAP:open", "PUT /carts/{id}")], \
    d2.transitions("cart")
print("OK   DSMI capability mask tracks lost capabilities, not probing order")

# ---------------------------------------------------------- cross-entity
g = RelationalKnowledgeGraph(seed=1)
g.add_entity("owner", 1)
g.add_entity("owner", 2)
g.add_entity("pet", 10, parent=("owner", 1))
g.add_entity("pet", 20, parent=("owner", 2))
cross = g.query_cross_entity_bindings("/owners/{ownerId}/pets/{petId}")
assert cross is not None and (cross["ownerId"], cross["petId"]) in {(1, 20), (2, 10)}, cross
print("OK   query_cross_entity_bindings produces a deliberate owner/pet mismatch (BOLA probe)")

lonely = RelationalKnowledgeGraph(seed=1)
lonely.add_entity("owner", 1)
lonely.add_entity("pet", 10, parent=("owner", 1))
assert lonely.query_cross_entity_bindings("/owners/{ownerId}/pets/{petId}") is None
print("OK   no mismatch invented when only one owner exists")

# -------------------------------------------------------------- mutators
ops = order_operations(parse_spec("specs/pet-clinic-openapi.json"))
by_key = {o.key: o for o in ops}
synth = SequenceSynthesizer(ops)
target = by_key["POST /petclinic/api/owners/{ownerId}/pets"]
plan = synth.synthesize(target, rkg=None)
mut = SequenceMutator(ops, seed=7)

m = mut.mutate(plan, name="omit_prerequisite")
assert m and len(m.ops) == len(plan) - 1 and m.ops[-1].key == target.key, m
print("OK   omit_prerequisite drops a setup step but keeps the target last")

m = mut.mutate(plan, name="invert_order")
assert m and len(m.ops) == len(plan) and [o.key for o in m.ops] != [o.key for o in plan]
print("OK   invert_order swaps two adjacent steps")

# cross_entity needs a parent id AND a child id in the path to mismatch, so it
# applies to a nested item route, not to a collection create under one parent.
nested = by_key["PUT /petclinic/api/owners/{ownerId}/pets/{petId}"]
nested_plan = synth.synthesize(nested, rkg=None)
m = mut.mutate(nested_plan, name="cross_entity")
assert m and m.cross_entity and len(m.ops) == len(nested_plan)
assert mut.mutate(plan, name="cross_entity") is None, \
    "a single-parameter path has no child id to mismatch"
print("OK   cross_entity flags a nested route, declines a single-parameter one")

m = mut.mutate(plan, name="double_action")
assert m and m.duplicate_last
assert mut.mutate([by_key["GET /petclinic/api/owners"]], name="double_action") is None, \
    "a safe GET is not a double-action candidate"
print("OK   double_action applies only to state-changing targets")

m = mut.mutate(plan, name="illegal_transition")
assert m and len(m.ops) == len(plan) + 1, m
print("OK   illegal_transition appends an operation on the same resource")

assert mut.mutate(plan) is not None, "a random mutation should apply to a 3-step plan"
assert set(OPERATORS) == {"omit_prerequisite", "invert_order", "illegal_transition",
                          "cross_entity", "double_action"}

# ----------------------------------------------- executing a mutated plan
class CountingClient:
    def __init__(self):
        self.sent = []
        self._ids = {"owner": 0, "pet": 100, "pettype": 50}

    async def send(self, req):
        self.sent.append(req)
        for res in ("pettype", "pet", "owner"):
            if req.path.rstrip("/").endswith(res + "s") and req.method == "POST":
                self._ids[res] += 1
                return Response(status=201, json={"id": self._ids[res]})
        return Response(status=200, json={"id": 1})


async def main():
    g = RelationalKnowledgeGraph(seed=1)
    d = DynamicStateMachineInferrer()
    client = CountingClient()
    ex = AsyncSequenceExecutor(client, generate_request, pool=None)

    # double_action must put two concurrent copies of the final step on the wire
    plan = synth.synthesize(target, rkg=g)
    m = mut.mutate(plan, name="double_action")
    seq = await ex.execute(m, g, dsmi=d)
    last_two = [s.request.op_key for s in seq.steps[-2:]]
    assert last_two == [target.key, target.key], last_two
    print("OK   executor fires the final step twice for double_action")

    # the DSMI saw the traffic the executor generated
    assert d.states_of("owner"), d.summary()
    print("OK   executor feeds observations into the DSMI")

    # omit_prerequisite still reaches the target with a broken prefix
    g2 = RelationalKnowledgeGraph(seed=1)
    client2 = CountingClient()
    ex2 = AsyncSequenceExecutor(client2, generate_request, pool=None)
    m2 = mut.mutate(synth.synthesize(target, rkg=g2), name="omit_prerequisite")
    seq2 = await ex2.execute(m2, g2, stop_on_prefix_failure=False)
    assert seq2.steps[-1].request.op_key == target.key, [s.request.op_key for s in seq2.steps]
    print("OK   omit_prerequisite still reaches the target without its setup step")


asyncio.run(main())
print("ALL CHECKS PASSED")
