"""RelationalKnowledgeGraph (RKG): tracks entities and parent/child relationships.

Milestone 1 of docs/architecture/track_a_stateful_fuzzing.md §4.1. This is
additive and standalone: nothing in engine/strategy.py uses it yet, so the
competition entry point is unaffected.

Motivation (see the doc): engine/pool.py stores harvested ids in flat,
per-resource-type deques. If the pool holds Owner 1, Owner 2, Pet 10 (owned by
1) and Pet 20 (owned by 2), a lookup for "/owners/{ownerId}/pets/{petId}" can
pair Owner 1 with Pet 20 -- a real id of the right *type*, but the wrong
*instance* -- which yields an uninformative 404 instead of exercising the
nested route. The RKG instead tracks which child belongs to which parent, so
``query_path_bindings`` can return a chain of ids that are actually related.

Naming conventions (normalised resource-type keys, e.g. "owner", "pet") are
kept consistent with engine/pool.py by reusing its helpers, so the two can
converge later without a second, diverging set of naming rules.
"""
from __future__ import annotations

import random
import re
import threading
import time
from typing import Any, Optional

from ..engine.pool import norm, singular, resource_for_param
from ..models import EntityEdge, EntityNode

_PARAM_RE = re.compile(r"\{([^}]+)\}")


class RelationalKnowledgeGraph:
    """In-memory store of EntityNodes and the EntityEdges between them."""

    def __init__(self, seed: Optional[int] = None):
        self._nodes: dict[tuple[str, Any], EntityNode] = {}
        self._children: dict[tuple[str, Any], set[tuple[str, Any]]] = {}
        self._edges: list[EntityEdge] = []
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    # ------------------------------------------------------------- mutation
    def add_entity(self, entity_type: str, entity_id: Any, attributes: Optional[dict] = None,
                    parent: Optional[tuple[str, Any]] = None, created_by_op: str = "",
                    state: Optional[str] = None, relation: str = "owns") -> EntityNode:
        """Register (or update) an entity and link it to its parent node.

        ``parent`` is a (entity_type, entity_id) tuple, already normalised or
        not -- it is normalised here too. If the parent hasn't been registered
        yet, a minimal stub node is created for it so the edge is never
        dangling.
        """
        key = (norm(singular(entity_type)), entity_id)
        with self._lock:
            node = self._nodes.get(key)
            if node is None:
                node = EntityNode(entity_type=key[0], entity_id=entity_id, state=state,
                                   attributes=dict(attributes or {}), created_by_op=created_by_op,
                                   timestamp=time.monotonic())
                self._nodes[key] = node
            else:
                node.attributes.update(attributes or {})
                if state is not None:
                    node.state = state
                node.deleted = False   # re-observing a "deleted" entity un-marks it (e.g. recreated)

            if parent is not None:
                pkey = (norm(singular(parent[0])), parent[1])
                if pkey not in self._nodes:
                    self._nodes[pkey] = EntityNode(entity_type=pkey[0], entity_id=pkey[1],
                                                    timestamp=time.monotonic())
                self._children.setdefault(pkey, set()).add(key)
                self._edges.append(EntityEdge(parent=pkey, child=key, relation=relation))
            return node

    def set_state(self, entity_type: str, entity_id: Any, state: str) -> None:
        with self._lock:
            node = self._nodes.get((norm(singular(entity_type)), entity_id))
            if node is not None:
                node.state = state

    def cascade_delete(self, entity_type: str, entity_id: Any) -> list[tuple[str, Any]]:
        """Mark the entity and all of its (recursive) children as deleted.

        Returns the list of (entity_type, entity_id) keys marked, for
        stale-reference / zombie-resource testing.
        """
        root = (norm(singular(entity_type)), entity_id)
        marked: list[tuple[str, Any]] = []
        with self._lock:
            stack = [root]
            seen: set = set()
            while stack:
                key = stack.pop()
                if key in seen:
                    continue
                seen.add(key)
                node = self._nodes.get(key)
                if node is not None:
                    node.deleted = True
                    marked.append(key)
                stack.extend(self._children.get(key, ()))
        return marked

    # --------------------------------------------------------------- lookup
    def get_entity(self, entity_type: str, entity_id: Any) -> Optional[EntityNode]:
        with self._lock:
            return self._nodes.get((norm(singular(entity_type)), entity_id))

    def children_of(self, entity_type: str, entity_id: Any,
                     child_type: Optional[str] = None) -> list[EntityNode]:
        with self._lock:
            keys = self._children.get((norm(singular(entity_type)), entity_id), ())
            out = [self._nodes[k] for k in keys if k in self._nodes]
        if child_type:
            ct = norm(singular(child_type))
            out = [n for n in out if n.entity_type == ct]
        return out

    def query_path_bindings(self, path_template: str, op: Any = None) -> Optional[dict[str, Any]]:
        """Resolve every ``{param}`` in ``path_template`` to a real, *related* id.

        Walks the path parameters left to right. The first parameter may bind
        to any live entity of its resource type; every parameter after it must
        bind to a *child* of the previously bound entity (not just any entity
        of the right type) -- e.g. for "/owners/{ownerId}/pets/{petId}",
        ``petId`` is resolved only from among the pets that actually belong to
        the bound owner. Returns ``None`` if no valid chain exists yet (the
        caller should fall back to generating fresh setup requests).
        """
        params = _PARAM_RE.findall(path_template)
        if not params:
            return {}
        bindings: dict[str, Any] = {}
        parent_key: Optional[tuple[str, Any]] = None
        with self._lock:
            for pname in params:
                rtype = resource_for_param(path_template, pname)
                if not rtype:
                    rtype = norm(singular(pname[:-2])) if pname.lower().endswith("id") else norm(pname)
                if parent_key is None:
                    candidates = [n for n in self._nodes.values()
                                  if n.entity_type == rtype and not n.deleted]
                else:
                    child_keys = self._children.get(parent_key, set())
                    candidates = [self._nodes[k] for k in child_keys
                                  if k in self._nodes and self._nodes[k].entity_type == rtype
                                  and not self._nodes[k].deleted]
                if not candidates:
                    return None
                node = self._pick(candidates)
                bindings[pname] = node.entity_id
                parent_key = node.key
        return bindings

    def query_cross_entity_bindings(self, path_template: str) -> Optional[dict[str, Any]]:
        """Like ``query_path_bindings``, but deliberately *mismatched*.

        Returns bindings whose final id is a real entity of the right type that
        belongs to a **different** parent -- Owner A's path with Owner B's pet.
        A correct API answers 404/403; one with Broken Object Level
        Authorization (BOLA) happily serves another tenant's object. Returns
        None when the graph doesn't yet hold two separately-owned entities to
        build the mismatch from.
        """
        params = _PARAM_RE.findall(path_template)
        if len(params) < 2:
            return None
        base = self.query_path_bindings(path_template)
        if base is None:
            return None
        last = params[-1]
        rtype = resource_for_param(path_template, last)
        own_id = base[last]
        with self._lock:
            parent_key = (resource_for_param(path_template, params[-2]), base[params[-2]])
            siblings = self._children.get(parent_key, set())
            foreign = [n for k, n in self._nodes.items()
                       if n.entity_type == rtype and not n.deleted
                       and k not in siblings and n.entity_id != own_id]
            if not foreign:
                return None
            victim = self._pick(foreign)
        out = dict(base)
        out[last] = victim.entity_id
        return out

    def _pick(self, candidates: list[EntityNode]) -> EntityNode:
        # recency-biased, like ResourcePool.lookup: prefer the most recently seen entity
        candidates.sort(key=lambda n: n.timestamp)
        return candidates[-1] if self._rng.random() < 0.7 else self._rng.choice(candidates)

    def live_types(self) -> set[str]:
        """Entity types with at least one entity that has not been deleted.

        Callers that match these against resource keys derived elsewhere should
        compare with spec/deps.py's ``keys_match`` rather than by equality: the
        two names are derived differently (a path segment here, a parameter
        name there) and disagree whenever English pluralisation rules mis-fire,
        e.g. the path ``/hospitais/`` yields "hospitai" while the parameter
        ``hospital_id`` yields "hospital".
        """
        with self._lock:
            return {n.entity_type for n in self._nodes.values() if not n.deleted}

    def has(self, entity_type: str, *, live_only: bool = True) -> bool:
        """Is at least one entity of this type known (and not deleted)?

        The sequence synthesizer uses this to decide whether a prerequisite is
        already satisfied or whether a producer operation must be prepended.
        """
        et = norm(singular(entity_type))
        with self._lock:
            return any(n.entity_type == et and not (live_only and n.deleted)
                       for n in self._nodes.values())

    # -------------------------------------------------------------- misc
    def size(self) -> int:
        with self._lock:
            return len(self._nodes)

    def snapshot(self, limit: int = 20) -> dict:
        with self._lock:
            items = list(self._nodes.items())[-limit:]
            return {f"{t}:{i}": {"state": n.state, "deleted": n.deleted, "attributes": n.attributes}
                    for (t, i), n in items}
