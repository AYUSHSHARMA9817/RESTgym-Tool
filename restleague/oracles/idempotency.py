"""RFC 9110 safety and idempotency oracles.

Milestone 3 of docs/architecture/track_a_stateful_fuzzing.md §4.5 (oracle 1).

RFC 9110 §9.2.1 makes GET/HEAD/OPTIONS *safe* (no state change) and §9.2.2
makes GET/HEAD/PUT/DELETE *idempotent* (N identical requests have the same
effect as one). Both are contractual promises that plenty of real APIs break --
a PUT that appends instead of replacing, a DELETE that 500s the second time,
a GET that mutates a counter.

All three oracles here are active: they re-send a request that already
succeeded and compare. Comparison always goes through ``state_diff``, which
drops volatile fields (timestamps, version counters) -- without that, every
API that stamps ``updatedAt`` on write would look broken.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from ..models import OracleViolation, Request, Sequence
from .base import Oracle, OracleContext, response_schema, state_diff

_SAFE = ("GET", "HEAD", "OPTIONS")


def _replay(req: Request) -> Request:
    """An identical copy, with the mutation tag cleared (it is not a mutant)."""
    clone = copy.deepcopy(req)
    clone.mutation = None
    return clone


class SafetyOracle(Oracle):
    """A safe method must not change what the next identical read returns."""
    name = "safety"
    active = True

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or req.method not in _SAFE or not resp.ok or req.mutation:
                continue
            if resp.json is None:
                continue
            again = await ctx.client.send(_replay(req))
            if not again.ok:
                if again.status >= 500:
                    out.append(self.violation(
                        req.op_key,
                        f"repeating a safe {req.method} returned {again.status}",
                        severity="violation", first=resp.status, second=again.status))
                continue
            op = ctx.op(req.op_key)
            schema = response_schema(op, resp.status) if op else {}
            diff = state_diff(resp.json, again.json, schema)
            if diff:
                out.append(self.violation(
                    req.op_key,
                    f"two identical {req.method}s returned different state: {diff[0]}",
                    severity="warning", differences=diff[:5]))
        return out


class IdempotencyOracle(Oracle):
    """PUT applied twice must leave the same state as applying it once."""
    name = "idempotency"
    active = True
    mutating = True        # its probe is a second PUT

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or req.method != "PUT" or not resp.ok or req.mutation:
                continue
            again = await ctx.client.send(_replay(req))
            if again.status >= 500:
                out.append(self.violation(
                    req.op_key,
                    f"a repeated identical PUT crashed with {again.status}",
                    severity="violation", first=resp.status, second=again.status))
                continue
            if not again.ok:
                out.append(self.violation(
                    req.op_key,
                    f"a repeated identical PUT changed outcome: {resp.status} then {again.status}",
                    severity="warning", first=resp.status, second=again.status))
                continue
            if resp.json is None or again.json is None:
                continue
            op = ctx.op(req.op_key)
            schema = response_schema(op, resp.status) if op else {}
            diff = state_diff(resp.json, again.json, schema)
            if diff:
                out.append(self.violation(
                    req.op_key,
                    f"PUT is not idempotent: {diff[0]}",
                    severity="violation", differences=diff[:5]))
        return out


class RepeatedDeleteOracle(Oracle):
    """A second DELETE of the same entity may 404 or 204 -- it must not crash."""
    name = "repeated_delete"
    active = True
    mutating = True        # its probe is a second DELETE

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or req.method != "DELETE" or not resp.ok or req.mutation:
                continue
            again = await ctx.client.send(_replay(req))
            if again.status >= 500:
                out.append(self.violation(
                    req.op_key,
                    f"deleting an already-deleted entity returned {again.status}",
                    severity="violation", first=resp.status, second=again.status))
        return out
