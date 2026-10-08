"""Shared contract between Person A (spec), Person B (engine) and Person C (main).

Change only with agreement from all three.

Flow:
    ops = parse_spec(path)                 # A
    ops = order_operations(ops)            # A
    run(ops, budget_seconds, base_url)     # B  (calls A's generate_request)

A's generator signature:
    generate_request(op: Operation, pool: ResourcePool, mode: Mode) -> Request

Inside the generator, A should call ``pool.lookup(param_name, schema, op)``
for path/query parameters and id-like body fields, and use the returned value
when it is not ``None``.
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
