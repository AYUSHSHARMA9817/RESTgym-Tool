"""Adaptive sequence guidance: which target to attack, and how to mutate it.

Milestone 4/5 of docs/architecture/track_a_stateful_fuzzing.md §4.6.

The doc frames this as MCTS over a tree of API states. In practice the tree is
unreachable: we cannot snapshot or restore the server's state, so we cannot
re-visit a node, which is what tree search needs. What we can do is the part
that actually pays -- treat target selection and mutation choice as two
coupled bandit problems over the doc's reward:

    R(sigma) = w1*d_coverage + w2*d_state_transitions + w3*I(violation) + w4*I(5XX)

The bandit is the discounted-UCB one already in engine/faults.py, reused rather
than reimplemented. Discounting matters here: a target stops paying once its
bugs are found, and the scheduler must notice and move on.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Optional

from ..engine.faults import Bandit
from ..engine.mutations import OPERATORS as PAYLOAD_OPERATORS
from ..models import Operation
from .mutator import OPERATORS


@dataclass
class Weights:
    """Reward weights, w1..w4 in the doc's R(sigma)."""
    coverage: float = 1.0           # w1: an operation got its first 2XX
    transition: float = 0.5         # w2: a state transition never seen before
    violation: float = 1.0          # w3: an oracle fired
    fault: float = 1.0              # w4: a new unique 5XX


@dataclass
class Outcome:
    """What one executed sequence produced; turned into a scalar reward."""
    new_coverage: int = 0
    new_transitions: int = 0
    new_violations: int = 0
    new_faults: int = 0
    prefix_failed: bool = False

    def reward(self, w: Weights) -> float:
        r = (w.coverage * self.new_coverage + w.transition * self.new_transitions
             + w.violation * self.new_violations + w.fault * self.new_faults)
        if self.prefix_failed and r == 0.0:
            r -= 0.05          # a chain that could not even be set up is weak evidence against it
        return r


class SequenceScheduler:
    """Chooses the next (target operation, mutation operator) pair."""

    def __init__(self, targets: list[Operation], *, seed: Optional[int] = None,
                 weights: Optional[Weights] = None, mutation_rate: float = 0.6,
                 attack_rate: float = 0.5, static_priors: Optional[dict] = None):
        self.targets = {op.key: op for op in targets}
        # per-operation prior from the synthesizer: operations that consume a
        # resource benefit most from a prefix, so they are preferred -- but the
        # others are still reachable, which excluding them outright prevented.
        self.static_priors = static_priors or {}
        self.weights = weights or Weights()
        self.mutation_rate = mutation_rate
        self.attack_rate = attack_rate
        self.slow_threshold = float(os.environ.get("SEQUENCE_SLOW_THRESHOLD", "0.5"))
        self.rng = random.Random(seed)
        self.target_bandit = Bandit(list(self.targets), c=0.7, seed=seed)
        self.mutation_bandit = Bandit(list(OPERATORS), c=0.8, gamma=0.99, seed=seed)
        # payload operators from the existing engine, applied to the final step
        self.attack_bandit = Bandit(list(PAYLOAD_OPERATORS), c=0.8, gamma=0.99, seed=seed)
        self.covered: set[str] = set()
        self.attempts: dict[str, int] = {k: 0 for k in self.targets}
        self.unreachable: dict[str, int] = {k: 0 for k in self.targets}
        # rolling mean seconds per sequence, per target (see _weights)
        self.cost: dict[str, float] = {}

    # ------------------------------------------------------------ choosing
    def next_target(self) -> Optional[Operation]:
        if not self.targets:
            return None
        key = self.target_bandit.select(weights=self._weights())
        self.attempts[key] = self.attempts.get(key, 0) + 1
        return self.targets[key]

    def _weights(self) -> dict:
        """Static priors layered on the bandit's learned scores."""
        out = {}
        for key, op in self.targets.items():
            w = 1.0
            if key not in self.covered:
                w += 0.5                     # unexercised: coverage is still available
            if op.method in ("POST", "PUT", "PATCH", "DELETE"):
                w += 0.5                     # writes change state; reads rarely crash
            misses = self.unreachable.get(key, 0)
            if misses:
                w *= 1.0 / (1.0 + 0.4 * misses)   # keeps failing to set up; back off
            w *= self.static_priors.get(key, 1.0)
            # Back off targets whose sequences are slow. engine/strategy.py has
            # had this since the start; without it a single slow endpoint eats
            # the budget, because sequences run one at a time. Measured on
            # gestao-hospital, POST /v1/hospitais/ took 2.26s against <10ms for
            # every other operation and consumed the entire run.
            secs = self.cost.get(key)
            if secs and secs > self.slow_threshold:
                w *= self.slow_threshold / secs
            out[key] = max(w, 0.02)
        return out

    def next_mutation(self) -> Optional[str]:
        """A structural mutation operator name, or None to run the plan as-is."""
        if self.rng.random() > self.mutation_rate:
            return None
        return self.mutation_bandit.select()

    def next_attack(self) -> Optional[str]:
        """A payload operator for the final step, or None to leave it valid.

        Separate from next_mutation: a sequence can be reshaped structurally
        *and* carry an attack payload, and the two are learned independently so
        a reward is not credited to whichever happened to be chosen alongside.
        """
        if self.rng.random() > self.attack_rate:
            return None
        return self.attack_bandit.select()

    # ------------------------------------------------------------ learning
    def note_cost(self, op_key: str, seconds: float) -> None:
        """Record how long a sequence for this target took (exponential mean)."""
        prev = self.cost.get(op_key)
        self.cost[op_key] = seconds if prev is None else (0.7 * prev + 0.3 * seconds)

    def update(self, target: Operation, mutation: Optional[str], outcome: Outcome,
               attack: Optional[str] = None) -> float:
        reward = outcome.reward(self.weights)
        self.target_bandit.update(target.key, reward)
        if mutation:
            self.mutation_bandit.update(mutation, reward)
        if attack:
            self.attack_bandit.update(attack, reward)
        if outcome.prefix_failed:
            self.unreachable[target.key] = self.unreachable.get(target.key, 0) + 1
        else:
            self.unreachable[target.key] = 0
        return reward

    def mark_covered(self, op_key: str) -> bool:
        """Returns True the first time an operation is covered."""
        if op_key in self.covered:
            return False
        self.covered.add(op_key)
        return True

    def stats(self) -> dict:
        return {
            "targets": len(self.targets),
            "covered": len(self.covered),
            "top_targets": sorted(
                ((k, round(self.target_bandit.score(k), 3)) for k in self.targets),
                key=lambda kv: -kv[1])[:5],
            "top_mutations": sorted(
                ((m, round(self.mutation_bandit.score(m), 3)) for m in OPERATORS),
                key=lambda kv: -kv[1])[:5],
            "top_attacks": sorted(
                ((a, round(self.attack_bandit.score(a), 3)) for a in PAYLOAD_OPERATORS),
                key=lambda kv: -kv[1])[:5],
        }
