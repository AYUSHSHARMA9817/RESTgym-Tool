"""SequenceSynthesizer: assemble the setup operations needed to reach a target.

Milestone 2 of docs/architecture/track_a_stateful_fuzzing.md §4.2.

A *plan* is an ordered list of Operations ending in the target, such that each
operation's prerequisites are produced by an earlier one:

    target = POST /owners/{ownerId}/pets/{petId}/visits
      needs petId   -> POST /owners/{ownerId}/pets
      needs ownerId -> POST /owners
    plan = [POST /owners, POST /owners/{ownerId}/pets, <target>]

Prerequisites already satisfied in the RelationalKnowledgeGraph are not
re-created: if a live owner is already known, no "POST /owners" step is added.
The producer/consumer analysis is the existing one in spec/deps.py -- this
module does not define a second, diverging notion of what a resource is.
"""
from __future__ import annotations

from typing import Optional

from ..models import Operation
from ..spec.deps import consumed_keys, keys_match, produced_keys

MIN_LEN = 2      # a "sequence" shorter than this is just a single request
MAX_LEN = 8      # doc §4.2: 2 <= k <= 8


def type_of_resource_key(key: str) -> str:
    """deps.py resource key -> RKG entity type: 'ownerid' -> 'owner'."""
    return key[:-2] if key.endswith("id") and len(key) > 2 else key


class SequenceSynthesizer:
    """Builds operation plans from the static dependency graph plus live RKG state."""

    def __init__(self, ops: list[Operation], max_len: int = MAX_LEN):
        self.ops = ops
        self.by_key = {o.key: o for o in ops}
        self.max_len = max_len
        self._producers: dict[str, Operation] = {}
        for op in ops:
            for key in produced_keys(op):
                # prefer the shallowest producer: POST /owners over POST /clinics/{id}/owners
                cur = self._producers.get(key)
                if cur is None or op.path.count("/") < cur.path.count("/"):
                    self._producers[key] = op

    # ------------------------------------------------------------- planning
    def synthesize(self, target: Operation, rkg=None) -> list[Operation]:
        """Return an ordered plan of Operations ending in ``target``.

        ``rkg``: a RelationalKnowledgeGraph. Prerequisites it already satisfies
        are skipped. Pass None to plan from an empty world (every prerequisite
        gets a producer step), which is what a cold start needs.
        """
        plan: list[Operation] = []
        self._resolve(target, rkg, plan, set())
        plan.append(target)
        if len(plan) > self.max_len:
            # keep the steps nearest the target; the earlier ones are the most
            # likely to be already satisfied in a long-running session anyway
            plan = plan[-self.max_len:]
        return plan

    def _resolve(self, op: Operation, rkg, plan: list[Operation], visiting: set) -> None:
        """Depth-first: append producers of everything ``op`` consumes, deepest first."""
        if op.key in visiting:
            return                       # cycle in the dependency graph; stop here
        visiting.add(op.key)
        live = self._live_keys(rkg)
        # sorted: consumed_keys returns a set, whose iteration order varies with
        # PYTHONHASHSEED. Without this the plan order changes between runs and a
        # seeded experiment stops being reproducible.
        for key in sorted(consumed_keys(op)):
            if any(keys_match(have, key) for have in live):
                continue                 # already have a live entity of this type
            producer = self._producer_for(key)
            if producer is None or producer.key == op.key:
                continue                 # nothing creates it; executor will have to improvise
            if any(p.key == producer.key for p in plan):
                continue                 # already scheduled
            self._resolve(producer, rkg, plan, visiting)
            if not any(p.key == producer.key for p in plan):
                plan.append(producer)
        visiting.discard(op.key)

    @staticmethod
    def _live_keys(rkg) -> set[str]:
        """Resource keys the graph can already satisfy, in deps.py's key form.

        Matched with ``keys_match`` rather than by equality because the two
        names come from different places and do not always agree: the graph
        names an entity after its path segment, the dependency analysis after
        the parameter that refers to it. Against a live gestao-hospital that
        gap meant a hospital already in the graph was never recognised, so
        every sequence re-created one through a 2.26s endpoint and the whole
        budget went on setup -- 159 requests in 90 seconds.
        """
        if rkg is None:
            return set()
        try:
            return {t + "id" for t in rkg.live_types()}
        except Exception:
            return set()

    def _producer_for(self, key: str) -> Optional[Operation]:
        """Exact match first, then deps.py's fuzzy resource-key matching."""
        if key in self._producers:
            return self._producers[key]
        matches = [k for k in self._producers if keys_match(k, key)]
        return self._producers[min(matches, key=len)] if matches else None

    # ---------------------------------------------------------------- misc
    def is_sequence(self, plan: list[Operation]) -> bool:
        """True when the plan is a real multi-step sequence, not a lone request."""
        return len(plan) >= MIN_LEN

    def targets(self) -> list[Operation]:
        """Every operation is a target.

        This used to return only operations that consume a resource, on the
        reasoning that those are the ones a synthesized prefix helps. That was
        wrong in practice: an operation nobody targets is never called at all,
        because prefixes only ever contain *producers*. Measured against a live
        pet-clinic, it left 8 of 35 operations completely untouched -- almost
        all of them plain list reads such as ``GET /owners``. Consumers are
        still preferred, by weight rather than by exclusion (``target_priors``).
        """
        return list(self.ops)

    def target_priors(self) -> dict[str, float]:
        """Relative priority per operation, for the scheduler's static weights.

        Operations that consume a resource gain the most from a synthesized
        prefix, so they stay the main focus; the rest are merely less frequent,
        not excluded.
        """
        return {op.key: (1.6 if consumed_keys(op) else 1.0) for op in self.ops}
