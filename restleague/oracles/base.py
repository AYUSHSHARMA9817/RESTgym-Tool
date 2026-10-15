"""Oracle base class and the tolerance helpers every oracle depends on.

Milestone 3 of docs/architecture/track_a_stateful_fuzzing.md §4.5.

The tolerance helpers are the important part. A naive "the two responses must be
equal" check false-positives on practically every real API, because almost all of
them bump an ``updatedAt`` timestamp or a ``version`` counter on write, and
because server-generated fields (ids, computed totals) are never echoed back
unchanged from what the client sent. ``stable_view`` strips those before any
comparison, so an oracle only ever compares what the client actually controls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..engine.pool import norm
from ..models import Operation, OracleViolation, Sequence

# Field names whose value is expected to change on its own between two otherwise
# identical reads. Compared after normalisation (lower-case, no separators).
VOLATILE_NAMES = frozenset({
    "updatedat", "updated", "updatedon", "updatedtime", "modifiedat", "modified",
    "lastmodified", "lastmodifieddate", "lastupdated", "lastupdatedat",
    "createdat", "created", "createdon", "createddate", "creationdate",
    "accessedat", "lastaccess", "lastaccessed", "expiresat", "expires", "expiry",
    "version", "etag", "revision", "rev", "_rev", "seq", "sequence",
    "timestamp", "ts", "servertime", "now", "date", "datetime",
    "nonce", "requestid", "traceid", "correlationid", "spanid",
    "duration", "elapsed", "took", "latency", "responsetime",
})


def is_volatile(name: str) -> bool:
    return norm(name) in VOLATILE_NAMES


def readonly_fields(schema: Optional[dict]) -> set[str]:
    """Top-level property names the schema marks ``readOnly`` (server-owned)."""
    if not schema:
        return set()
    return {k for k, v in (schema.get("properties") or {}).items()
            if isinstance(v, dict) and v.get("readOnly")}


def stable_view(value: Any, schema: Optional[dict] = None, ignore: frozenset = frozenset(),
                _depth: int = 0) -> Any:
    """``value`` with volatile, readOnly and explicitly ignored fields removed.

    Two stable views can be compared directly: anything left is something the
    client set or the server is contractually meant to keep constant.
    """
    if _depth > 6:
        return value
    if isinstance(value, dict):
        ro = readonly_fields(schema)
        props = (schema or {}).get("properties") or {}
        out = {}
        for k, v in value.items():
            if is_volatile(k) or k in ro or norm(k) in {norm(i) for i in ignore}:
                continue
            out[k] = stable_view(v, props.get(k) if isinstance(props.get(k), dict) else None,
                                 ignore, _depth + 1)
        return out
    if isinstance(value, list):
        items = (schema or {}).get("items") if isinstance(schema, dict) else None
        return [stable_view(v, items if isinstance(items, dict) else None, ignore, _depth + 1)
                for v in value]
    return value


def state_diff(a: Any, b: Any, schema: Optional[dict] = None,
               ignore: frozenset = frozenset()) -> list[str]:
    """Field paths where two stable views differ. Empty list == equivalent."""
    return _diff(stable_view(a, schema, ignore), stable_view(b, schema, ignore), "$")


def _diff(a: Any, b: Any, where: str, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for k in set(a) | set(b):
            if k not in a:
                out.append(f"{where}.{k}: added")
            elif k not in b:
                out.append(f"{where}.{k}: removed")
            else:
                out += _diff(a[k], b[k], f"{where}.{k}", depth + 1)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{where}: length {len(a)} -> {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{where}[{i}]", depth + 1)
        return out
    if a != b:
        return [f"{where}: {_short(a)} -> {_short(b)}"]
    return []


def _short(v: Any, limit: int = 60) -> str:
    s = repr(v)
    return s if len(s) <= limit else s[:limit] + "..."


def response_schema(op: Operation, status: int) -> dict:
    """The declared schema for this status, falling back to the 2XX default."""
    responses = op.responses or {}
    for key in (str(status), f"{status // 100}XX", f"{status // 100}xx", "default"):
        if key in responses and responses[key]:
            return responses[key]
    return {}


@dataclass
class OracleContext:
    """Everything an oracle may need beyond the sequence itself."""
    ops: dict[str, Operation] = field(default_factory=dict)   # Operation.key -> Operation
    client: Any = None              # engine.client.ApiClient; required by active oracles
    rkg: Any = None                 # RelationalKnowledgeGraph, when available
    zombie_retries: int = 3         # bounded re-reads before flagging a zombie (eventual consistency)
    # Seconds, doubled per retry. Deliberately small: the loop is sequential,
    # so every sleep here stalls the whole run. At 0.25s the ladder cost 0.75s
    # per zombie check and measurably starved sequence throughput; 0.05s still
    # absorbs the replica lag this is meant to tolerate, at a seventh the cost.
    zombie_backoff: float = 0.05
    enabled: bool = True

    def op(self, key: str) -> Optional[Operation]:
        return self.ops.get(key)


class Oracle:
    """Base class. ``check`` inspects executed steps.

    Oracles with ``active = True`` send extra HTTP requests of their own (a
    second PUT, a GET after DELETE) and need ``ctx.client``; they must be
    skipped when no client is available.

    ``mutating`` says whether those extra requests *change server state*. It
    decides when an oracle may run. A non-mutating oracle is checked inline,
    immediately after the step it judges -- which is the only way to attribute
    a change to the right step, since a later step in the same sequence can
    change the very state the oracle is about to re-read. A mutating oracle
    (a second PUT, a second DELETE) would wreck the rest of the chain if fired
    mid-sequence, so it runs only on the final step.
    """
    name = "oracle"
    active = False
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        raise NotImplementedError

    def violation(self, op_key: str, summary: str, *, severity: str = "violation",
                  **evidence: Any) -> OracleViolation:
        return OracleViolation(oracle=self.name, op_key=op_key, summary=summary,
                               severity=severity, evidence=evidence)
