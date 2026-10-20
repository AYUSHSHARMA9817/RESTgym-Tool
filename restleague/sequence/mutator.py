"""SequenceMutator: structural mutations applied to a whole plan, not one request.

Milestone 4 of docs/architecture/track_a_stateful_fuzzing.md §4.4. Where
engine/mutations.py corrupts the *contents* of a single request (a bad id, a
huge int, an injection payload), this corrupts the *shape of the chain*: what
runs, in what order, how many times, and against whose data.

| operator              | what it does                               | hunts for              |
|-----------------------|--------------------------------------------|------------------------|
| omit_prerequisite     | drops a setup step                         | missing precondition checks |
| invert_order          | swaps two adjacent steps                   | out-of-order handling  |
| illegal_transition    | appends an op never seen from this state   | unchecked state machine |
| cross_entity          | binds the target to another parent's child | BOLA / tenant isolation |
| double_action         | fires the final step twice, concurrently   | races, double-spend    |

Each operator returns a ``PlanMutation`` (or None when it does not apply). Two
of them cannot be expressed by reordering operations alone, so the mutation
carries execution flags that AsyncSequenceExecutor honours.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models import Operation
from ..spec.deps import consumed_keys
from .synthesizer import type_of_resource_key


@dataclass
class PlanMutation:
    """A mutated plan plus the execution flags the executor needs."""
    ops: list[Operation]
    name: str
    cross_entity: bool = False      # bind the final step's id from a different parent
    duplicate_last: bool = False    # send the final step twice, concurrently
    note: str = ""

    @property
    def target(self) -> Optional[Operation]:
        return self.ops[-1] if self.ops else None


def omit_prerequisite(plan: list[Operation], rng: random.Random, **_) -> Optional[PlanMutation]:
    """Drop one setup step and call the target anyway."""
    if len(plan) < 2:
        return None
    i = rng.randrange(len(plan) - 1)
    dropped = plan[i]
    return PlanMutation([op for j, op in enumerate(plan) if j != i], "omit_prerequisite",
                        note=f"dropped {dropped.key}")


def invert_order(plan: list[Operation], rng: random.Random, **_) -> Optional[PlanMutation]:
    """Swap two adjacent steps so a consumer runs before its producer."""
    if len(plan) < 2:
        return None
    i = rng.randrange(len(plan) - 1)
    ops = list(plan)
    ops[i], ops[i + 1] = ops[i + 1], ops[i]
    return PlanMutation(ops, "invert_order", note=f"swapped {plan[i].key} and {plan[i+1].key}")


def cross_entity(plan: list[Operation], rng: random.Random, **_) -> Optional[PlanMutation]:
    """Keep the plan, but point the last step at another parent's child."""
    if not plan:
        return None
    target = plan[-1]
    if target.path.count("{") < 2:
        return None          # needs a parent id and a child id to mismatch
    return PlanMutation(list(plan), "cross_entity", cross_entity=True,
                        note=f"foreign id in {target.key}")


def double_action(plan: list[Operation], rng: random.Random, **_) -> Optional[PlanMutation]:
    """Fire the final (state-changing) step twice at once."""
    if not plan or plan[-1].method in ("GET", "HEAD", "OPTIONS"):
        return None
    return PlanMutation(list(plan), "double_action", duplicate_last=True,
                        note=f"concurrent duplicate of {plan[-1].key}")


def illegal_transition(plan: list[Operation], rng: random.Random, *, dsmi=None, rkg=None,
                       ops: Optional[list[Operation]] = None, **_) -> Optional[PlanMutation]:
    """Append an operation never observed from the target entity's current state.

    With a DSMI available this is state-directed (e.g. "pay an order that is
    already PAID"). Without one it falls back to any other operation consuming
    the same resource, which is a weaker but still useful approximation.
    """
    if not plan or not ops:
        return None
    target = plan[-1]
    wanted = {type_of_resource_key(k) for k in consumed_keys(target)}
    if not wanted:
        return None
    siblings = [op for op in ops
                if op.key != target.key
                and op.method not in ("GET", "HEAD", "OPTIONS")
                and {type_of_resource_key(k) for k in consumed_keys(op)} & wanted]
    if not siblings:
        return None

    # State-directed when possible: resolve the entity this plan will actually
    # bind, read *its* state, and pick an operation never seen from that state.
    # Guessing a state at random from the type would make the "illegal"
    # transition no better than appending an arbitrary write.
    if dsmi is not None:
        state, etype = _bound_state(target, rkg, dsmi)
        if state is not None:
            unseen = dsmi.unseen_operations(etype, state, siblings)
            if unseen:
                nxt = rng.choice(unseen)
                return PlanMutation(list(plan) + [nxt], "illegal_transition",
                                    note=f"{nxt.key} never observed from {etype} state {state}")
    nxt = rng.choice(siblings)
    return PlanMutation(list(plan) + [nxt], "illegal_transition",
                        note=f"{nxt.key} appended after {target.key} (state unknown)")


def _bound_state(target: Operation, rkg, dsmi) -> tuple[Optional[str], str]:
    """(state, entity_type) of the entity ``target`` would act on, if known."""
    if rkg is None:
        return None, ""
    try:
        bindings = rkg.query_path_bindings(target.path)
    except Exception:
        return None, ""
    if not bindings:
        return None, ""
    from ..engine.pool import resource_for_param
    last = list(bindings)[-1]
    etype = resource_for_param(target.path, last) or ""
    if not etype:
        return None, ""
    state = dsmi.state_of(etype, bindings[last])
    return (state if state and state != "?" else None), etype


OPERATORS: dict[str, Callable[..., Optional[PlanMutation]]] = {
    "omit_prerequisite": omit_prerequisite,
    "invert_order": invert_order,
    "illegal_transition": illegal_transition,
    "cross_entity": cross_entity,
    "double_action": double_action,
}


class SequenceMutator:
    """Picks and applies a structural mutation to a plan."""

    def __init__(self, ops: Optional[list[Operation]] = None, seed: Optional[int] = None):
        self.ops = ops or []
        self.rng = random.Random(seed)

    def mutate(self, plan: list[Operation], *, name: Optional[str] = None,
               dsmi=None, rkg=None) -> Optional[PlanMutation]:
        """Apply ``name`` (or a random applicable operator) to ``plan``."""
        names = [name] if name else list(OPERATORS)
        if not name:
            self.rng.shuffle(names)
        for n in names:
            fn = OPERATORS.get(n)
            if fn is None:
                continue
            mutation = fn(plan, self.rng, dsmi=dsmi, rkg=rkg, ops=self.ops)
            if mutation is not None:
                return mutation
        return None
