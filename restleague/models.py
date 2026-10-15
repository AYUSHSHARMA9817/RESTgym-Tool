"""Shared data types for the tool.

These dataclasses are the common interface between the spec parser/generator,
the execution engine, and the entrypoint. Keep them stable; changing a field
affects every part.

Flow:
    ops = parse_spec(path)
    ops = order_operations(ops)
    run(ops, budget_seconds, base_url)      # the engine; calls generate_request

generate_request(op: Operation, pool: ResourcePool, mode: Mode) -> Request
Inside the generator, call ``pool.lookup(param_name, schema, op)`` for
path/query parameters and id-like body fields, and use the returned value when
it is not ``None``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class Mode(str, enum.Enum):
    VALID = "valid"        # aim for 2XX
    BOUNDARY = "boundary"  # min-1 / max+1 / empty / huge values
    INVALID = "invalid"    # wrong types, missing required, garbage


# ---------------------------------------------------------------- spec model
@dataclass
class Parameter:
    name: str
    location: str                       # "path" | "query" | "header" | "cookie" | "formData"
    required: bool = False
    schema: dict = field(default_factory=dict)   # fully $ref-resolved JSON schema
    style: Optional[str] = None
    explode: Optional[bool] = None


@dataclass
class RequestBody:
    schema: dict = field(default_factory=dict)   # resolved schema of the preferred media type
    content_types: list[str] = field(default_factory=lambda: ["application/json"])
    required: bool = False
    # optional per-media-type schemas, e.g. {"application/json": {...}, "application/xml": {...}}
    schemas: dict[str, dict] = field(default_factory=dict)


@dataclass
class Operation:
    method: str                         # upper-case: GET, POST, ...
    path: str                           # template, e.g. "/owners/{ownerId}" (base path included)
    operation_id: str = ""
    parameters: list[Parameter] = field(default_factory=list)
    request_body: Optional[RequestBody] = None
    responses: dict[str, dict] = field(default_factory=dict)  # status -> resolved schema ({} if none)
    tags: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)          # response media types
    security: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable identifier used everywhere (coverage, faults, logs)."""
        return f"{self.method.upper()} {self.path}"

    def params_in(self, location: str) -> list[Parameter]:
        return [p for p in self.parameters if p.location == location]

    def __hash__(self) -> int:
        return hash(self.key)


# ---------------------------------------------------------- concrete traffic
@dataclass
class Request:
    method: str
    path: str                                   # template, NOT yet filled ("/owners/{ownerId}")
    path_params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)   # list values => repeated keys
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    form: dict[str, Any] = field(default_factory=dict)    # formData / urlencoded / multipart fields
    body: Any = None                             # JSON-serialisable body (dict/list/scalar)
    content_type: Optional[str] = None           # e.g. "application/json"
    raw_body: Optional[bytes] = None             # if set, sent verbatim (overrides body/form)
    op_key: str = ""                             # Operation.key this request belongs to
    mutation: Optional[str] = None               # name of the mutation applied (engine only)


@dataclass
class Response:
    status: int                                  # 0 => no HTTP response (connection error/timeout)
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    json: Any = None                             # parsed JSON body or None
    elapsed: float = 0.0
    error: Optional[str] = None                  # transport error description when status == 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_5xx(self) -> bool:
        return 500 <= self.status < 600


# --------------------------------------------- Track A: sequence & state model
# Additive data model for the stateful-fuzzing track
# (docs/architecture/track_a_stateful_fuzzing.md). Nothing in spec/ or in the
# default engine (engine/strategy.py's `run`) depends on these; they exist for
# restleague/state/, restleague/sequence/ and restleague/oracles/, which are
# opt-in extensions, not part of the competition entry point.

@dataclass
class EntityNode:
    """One tracked API entity, e.g. the pet with id 10 owned by owner 1."""
    entity_type: str                    # normalised singular resource name, e.g. "pet"
    entity_id: Any
    state: Optional[str] = None         # inferred lifecycle state; filled in by a future DSMI
    attributes: dict = field(default_factory=dict)
    created_by_op: str = ""             # Operation.key that produced this entity
    timestamp: float = 0.0
    deleted: bool = False               # set by cascade_delete; kept around for stale-ref testing

    @property
    def key(self) -> tuple[str, Any]:
        return (self.entity_type, self.entity_id)


@dataclass
class EntityEdge:
    """A relational edge between two tracked entities, e.g. Owner(1) --owns--> Pet(10)."""
    parent: tuple[str, Any]
    child: tuple[str, Any]
    relation: str = "owns"


@dataclass
class Step:
    """One request/response pair inside a Sequence."""
    request: Request
    response: Optional[Response] = None
    bindings: dict[str, Any] = field(default_factory=dict)   # path params resolved from the RKG


@dataclass
class Sequence:
    """An ordered chain of Steps assembled to reach a target operation (2 <= len <= 8)."""
    steps: list[Step] = field(default_factory=list)
    target_op_key: str = ""

    def append(self, step: Step) -> None:
        self.steps.append(step)

    def __len__(self) -> int:
        return len(self.steps)


@dataclass
class OracleViolation:
    """A semantic bug flagged by a metamorphic oracle (distinct from a 5XX crash)."""
    oracle: str                          # e.g. "idempotency", "zombie_resource"
    op_key: str
    summary: str
    severity: str = "warning"            # "warning" | "violation"
    evidence: dict = field(default_factory=dict)
