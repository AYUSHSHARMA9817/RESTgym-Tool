"""Check RelationalKnowledgeGraph: entities link to the right parent, not just
the right type (docs/architecture/track_a_stateful_fuzzing.md, Milestone 1).

Run: python3 -m tests.check_state_graph
"""
from restleague.state.graph import RelationalKnowledgeGraph

g = RelationalKnowledgeGraph(seed=1)

# Owner 1 has Pet 10; Owner 2 has Pet 20 (the doc's motivating example).
g.add_entity("owner", 1, {"name": "George"})
g.add_entity("owner", 2, {"name": "Betty"})
g.add_entity("pet", 10, {"name": "Leo"}, parent=("owner", 1))
g.add_entity("pet", 20, {"name": "Max"}, parent=("owner", 2))

assert g.size() == 4, g.size()
assert g.get_entity("owner", 1).attributes["name"] == "George"
assert [n.entity_id for n in g.children_of("owner", 1)] == [10]
assert [n.entity_id for n in g.children_of("owner", 2)] == [20]

# query_path_bindings must never pair Owner 1 with Pet 20 or vice versa.
for _ in range(50):
    b = g.query_path_bindings("/owners/{ownerId}/pets/{petId}")
    assert b is not None, "expected a binding once pets exist"
    owner_id, pet_id = b["ownerId"], b["petId"]
    assert (owner_id, pet_id) in {(1, 10), (2, 20)}, b
print("OK   query_path_bindings never cross-wires Owner 1 with Pet 20")

# A single-parameter path just needs a live entity of that type.
b = g.query_path_bindings("/owners/{ownerId}")
assert b == {"ownerId": 1} or b == {"ownerId": 2}, b

# No entities of a type yet -> no binding (caller should fall back to a fresh setup sequence).
assert g.query_path_bindings("/visits/{visitId}") is None
print("OK   no binding returned when the graph has nothing of that type yet")

# cascade_delete marks the entity AND its children, recursively, and the
# deleted entity drops out of future bindings but its node is kept (for
# stale-reference / zombie-resource testing later, per the doc).
g.add_entity("visit", 100, {"description": "checkup"}, parent=("pet", 10))
marked = g.cascade_delete("owner", 1)
assert set(marked) == {("owner", 1), ("pet", 10), ("visit", 100)}, marked
assert g.get_entity("owner", 1).deleted is True
assert g.get_entity("pet", 10).deleted is True
assert g.get_entity("visit", 100).deleted is True
assert g.get_entity("owner", 2).deleted is False

for _ in range(20):
    b = g.query_path_bindings("/owners/{ownerId}/pets/{petId}")
    assert b == {"ownerId": 2, "petId": 20}, b
print("OK   cascade_delete marks children recursively; deleted entities excluded from bindings")

# re-observing a deleted entity (e.g. the API recreated it) un-marks it.
g.add_entity("owner", 1, {"name": "George"})
assert g.get_entity("owner", 1).deleted is False
print("OK   re-adding a deleted entity clears its deleted flag")

# Dangling parent: a child registered before its parent still links correctly.
g2 = RelationalKnowledgeGraph(seed=2)
g2.add_entity("pet", 99, parent=("owner", 7))
assert g2.get_entity("owner", 7) is not None, "stub parent should be auto-created"
assert [n.entity_id for n in g2.children_of("owner", 7)] == [99]
print("OK   a child registered before its parent still produces a correct edge")

print("ALL CHECKS PASSED")
