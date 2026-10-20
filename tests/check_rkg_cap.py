"""Checks for the optional RKG soft cap (restleague/state/graph.py).

The cap is a safety net for long unattended runs: the knowledge graph otherwise
grows with every created entity. Default is unbounded (exact prior behaviour);
RKG_MAX_NODES (or max_nodes=) turns on oldest-leaf eviction that never orphans a
live binding chain.

Run: python3 -m tests.check_rkg_cap
"""
from restleague.state.graph import RelationalKnowledgeGraph

# --------------------------------------------------- default is unbounded
g = RelationalKnowledgeGraph()
assert g.max_nodes == 0, "default cap must be 0 (unbounded) -- no behaviour change"
for i in range(200):
    g.add_entity("owner", i)
assert g.size() == 200, "with no cap every entity is kept"
assert g.evicted == 0
print("OK   default: unbounded, nothing evicted (baseline unchanged)")

# ------------------------------------------------- cap evicts oldest leaves
g = RelationalKnowledgeGraph(max_nodes=10)
for i in range(50):
    g.add_entity("owner", i)
assert g.size() <= 10, f"cap not enforced: {g.size()}"
assert g.evicted > 0, "nothing was evicted despite exceeding the cap"
# the survivors are the most recent ids (oldest evicted first)
survivors = {nid for (_t, nid) in g._nodes}
assert max(survivors) == 49, "the newest entity must survive"
assert 0 not in survivors, "the oldest entity should have been evicted"
print(f"OK   cap enforced: size={g.size()} evicted={g.evicted}, newest kept")

# ---------------------------------------- a parent with children is spared
g = RelationalKnowledgeGraph(max_nodes=5)
g.add_entity("owner", 1)                       # the parent, added first (oldest)
for i in range(100, 140):                      # many pets owned by owner 1
    g.add_entity("pet", i, parent=("owner", 1))
# owner 1 has live children, so it must not be evicted even though it is oldest
assert ("owner", 1) in g._nodes, "a parent with live children must not be evicted"
assert g.size() <= 5 + 1, "cap still roughly bounds the graph"
print("OK   a parent with live children is never evicted (chains stay intact)")

print("ALL CHECKS PASSED")
