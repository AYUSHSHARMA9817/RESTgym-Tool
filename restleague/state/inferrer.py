"""DynamicStateMachineInferrer (DSMI): learn each entity type's lifecycle.

Milestone 4 of docs/architecture/track_a_stateful_fuzzing.md §4.3.

The API never tells us its state machine, so we reconstruct one from traffic.
Each observed entity gets a **state signature** built from two sources:

1. **Explicit attributes** -- the value of a field named status / state / phase
   / stage / flag, e.g. ``"PAID"``. Cheap and precise when present.
2. **Capability mask** -- which methods the entity has *stopped* accepting,
   e.g. ``CAP:open`` then ``CAP:-PUT``. This is the fallback for the many APIs
   that expose no status field at all; a cancelled order that stops accepting
   PUT has observably changed state even though nothing in the body says so.
   The mask deliberately reports capabilities **lost** rather than capabilities
   held: keying on what is accepted conflates "we have not tried PUT yet" with
   "PUT is refused", so the signature would change the first time we probed any
   new method and the inferrer would record transitions that are artefacts of
   our own probing order. See ``_signature``.

From the signatures we build a Mealy-style transition relation
``(state, operation) -> state`` with observation counts. Its real purpose is
the inverse: ``unseen_operations`` reports what has *never* been observed from
a state, which is where the illegal-transition mutator aims.
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from typing import Any, Iterable, Optional

from ..engine.pool import norm, singular
from ..models import Operation, Request, Response

STATE_FIELD = re.compile(r"^(status|state|phase|stage|flag|condition|lifecycle)$", re.I)
CAPABILITY_METHODS = ("GET", "PUT", "PATCH", "DELETE", "POST")
UNKNOWN = "?"          # nothing observed about this entity yet
OPEN = "CAP:open"      # probed, and nothing has been refused so far


def explicit_state(attributes: Any) -> Optional[str]:
    """The lifecycle value carried in the body, if the body advertises one."""
    if not isinstance(attributes, dict):
        return None
    for k, v in attributes.items():
        if STATE_FIELD.match(str(k)) and isinstance(v, (str, bool, int)) and not isinstance(v, float):
            return str(v).upper()
    # one level down: {"order": {"status": "PAID"}}
    for v in attributes.values():
        if isinstance(v, dict):
            nested = explicit_state(v)
            if nested:
                return nested
    return None


class DynamicStateMachineInferrer:
    """Observes traffic, infers per-entity states and the transitions between them."""

    def __init__(self) -> None:
        # (entity_type, entity_id) -> last known state signature
        self._state: dict[tuple[str, Any], str] = {}
        # (entity_type, entity_id) -> {method: accepted?}
        self._caps: dict[tuple[str, Any], dict[str, bool]] = defaultdict(dict)
        # entity_type -> {(from_state, op_key): {to_state: count}}
        self._transitions: dict[str, dict[tuple[str, str], dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int)))
        self._ops_by_type: dict[str, set] = defaultdict(set)
        self._lock = threading.Lock()

    # ----------------------------------------------------------- observing
    def observe(self, op: Operation, req: Request, resp: Response,
                entity: Optional[tuple[str, Any]] = None) -> Optional[str]:
        """Record one interaction. Returns the entity's new state, if known.

        ``entity`` is the (type, id) the operation acted on; when omitted the
        call is only used to update the capability mask of nothing in
        particular, so callers should pass it whenever the RKG knows it.
        """
        if entity is None or resp.status == 0:
            return None
        etype = norm(singular(entity[0]))
        key = (etype, entity[1])
        method = op.method.upper()

        with self._lock:
            # capability mask: did this method work on this entity?
            if method in CAPABILITY_METHODS:
                self._caps[key][method] = resp.ok
            before = self._state.get(key, UNKNOWN)
            after = self._signature(key, resp)
            self._state[key] = after
            self._ops_by_type[etype].add(op.key)
            if resp.ok or before != after:
                self._transitions[etype][(before, op.key)][after] += 1
            return after

    def _signature(self, key: tuple[str, Any], resp: Response) -> str:
        """Explicit status wins; otherwise describe the entity by what it has *lost*.

        The fallback deliberately reports rejected methods, not accepted ones.
        Keying on accepted methods conflates "we have not tried PUT yet" with
        "PUT is refused", so the signature would change the first time we probe
        any new method and the inferrer would record a transition that is an
        artefact of our own probing order rather than anything the server did.
        Capabilities that have been *lost* are the real signal: an order that
        stops accepting PUT has genuinely changed state.
        """
        explicit = explicit_state(resp.json)
        if explicit:
            return explicit
        caps = self._caps.get(key, {})
        if not caps:
            return UNKNOWN
        rejected = sorted(m for m, ok in caps.items() if not ok)
        return ("CAP:-" + "+".join(rejected)) if rejected else OPEN

    # ------------------------------------------------------------ querying
    def state_of(self, entity_type: str, entity_id: Any) -> str:
        with self._lock:
            return self._state.get((norm(singular(entity_type)), entity_id), UNKNOWN)

    def states_of(self, entity_type: str) -> set[str]:
        et = norm(singular(entity_type))
        with self._lock:
            return {s for (t, _), s in self._state.items() if t == et}

    def transitions(self, entity_type: str) -> dict[tuple[str, str], dict[str, int]]:
        et = norm(singular(entity_type))
        with self._lock:
            return {k: dict(v) for k, v in self._transitions.get(et, {}).items()}

    def seen_operations(self, entity_type: str, state: str) -> set[str]:
        """Operation keys ever observed succeeding from this state."""
        et = norm(singular(entity_type))
        with self._lock:
            return {op_key for (frm, op_key), tos in self._transitions.get(et, {}).items()
                    if frm == state and any(tos.values())}

    def unseen_operations(self, entity_type: str, state: str,
                          candidates: Iterable[Operation]) -> list[Operation]:
        """Candidate operations never observed from this state.

        These are the illegal-transition probes: applying one is either a
        legitimate path nobody exercised yet, or a transition the server should
        reject -- and frequently doesn't.
        """
        seen = self.seen_operations(entity_type, state)
        return [op for op in candidates if op.key not in seen]

    def summary(self) -> dict:
        with self._lock:
            return {
                etype: {
                    "states": sorted({s for (t, _), s in self._state.items() if t == etype}),
                    "transitions": {f"{frm} --{op}-->": dict(tos)
                                    for (frm, op), tos in trans.items()},
                }
                for etype, trans in self._transitions.items()
            }
