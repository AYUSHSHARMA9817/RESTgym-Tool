"""Contract conformance oracle: does the response match what the spec promised?

Milestone 3 of docs/architecture/track_a_stateful_fuzzing.md §4.5 (oracle 4).

Two distinct bugs are detected here:

1. **Schema drift** -- a 2XX response whose body violates the schema the
   OpenAPI document declares for that status (wrong type, missing required
   field, value outside the declared range). The server is lying about its own
   contract, which breaks every generated client.
2. **Disguised exceptions** -- a stack trace or raw exception text returned
   under a 200 or 400 status. The status code says "fine" / "your fault", but
   the body is an unhandled server-side crash. These never show up in a
   5XX-only fault counter, which is exactly why this oracle exists.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import OracleViolation, Sequence
from .base import Oracle, OracleContext, response_schema
from .schema import violations as schema_violations

# Signatures of a leaked stack trace / unhandled exception, in several runtimes.
_TRACE_SIGNS = (
    re.compile(r"\bat [\w$.]+\([\w$.]+\.java:\d+\)"),            # Java
    re.compile(r"Traceback \(most recent call last\)"),           # Python
    re.compile(r"\b(?:java|org|com)\.[\w.]+(?:Exception|Error)\b"),
    re.compile(r"\b\w+(?:Exception|Error)\b:?\s", re.I),
    re.compile(r"\bat [\w.<>]+ in [\w:\\/.]+:line \d+"),           # .NET
    re.compile(r"\n\s+at .+\(.+:\d+:\d+\)"),                      # Node
    re.compile(r"(?i)\b(?:SQLSTATE|SQLException|ORA-\d{5}|ERROR:\s+syntax error at)\b"),
)
_MAX_BODY = 4000


def looks_like_stack_trace(text: str) -> bool:
    if not text or len(text) < 20:
        return False
    sample = text[:_MAX_BODY]
    return any(rx.search(sample) for rx in _TRACE_SIGNS)


class SchemaConformanceOracle(Oracle):
    """Passive: validates bodies already collected in the sequence."""
    name = "schema_conformance"
    active = False

    #: statuses whose bodies are validated against the declared response schema
    VALIDATE_RANGE = range(200, 300)

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or resp.status == 0:
                continue
            op = ctx.op(req.op_key)

            # 1. a crash disguised under a non-5XX status
            if resp.status < 500 and looks_like_stack_trace(resp.text):
                out.append(self.violation(
                    req.op_key,
                    f"stack trace leaked in a {resp.status} response",
                    severity="violation", status=resp.status,
                    excerpt=resp.text[:300], mutation=req.mutation))
                continue

            # 2. schema drift on a success response
            if op is None or resp.status not in self.VALIDATE_RANGE or resp.json is None:
                continue
            schema = response_schema(op, resp.status)
            if not schema:
                continue
            problems = schema_violations(resp.json, schema)
            if problems:
                out.append(self.violation(
                    req.op_key,
                    f"{resp.status} response violates its declared schema: {problems[0]}",
                    severity="warning", status=resp.status,
                    problems=problems[:5], mutation=req.mutation))
        return out
