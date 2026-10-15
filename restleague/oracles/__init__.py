"""Track A metamorphic & semantic oracles.

See docs/architecture/track_a_stateful_fuzzing.md §4.5. These look for bugs that
are *not* 5XX crashes: broken idempotency, state that survives deletion, writes
that silently don't persist, responses that violate the declared schema.

Additive: nothing in the competition entry point imports this package.
"""
from .authorization import CrossEntityAccessOracle
from .base import Oracle, OracleContext, stable_view, state_diff
from .conformance import SchemaConformanceOracle
from .crud import CrudRoundtripOracle, ZombieResourceOracle
from .idempotency import IdempotencyOracle, RepeatedDeleteOracle, SafetyOracle
from .massassign import MassAssignmentOracle

ALL_ORACLES = (
    SchemaConformanceOracle,
    CrossEntityAccessOracle,
    MassAssignmentOracle,
    CrudRoundtripOracle,
    ZombieResourceOracle,
    SafetyOracle,
    IdempotencyOracle,
    RepeatedDeleteOracle,
)
