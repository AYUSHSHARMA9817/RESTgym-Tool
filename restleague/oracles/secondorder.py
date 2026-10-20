"""Second-order injection (B3): a payload stored by one request breaks another.

This is the bug a stateful tester can find that a single-request fuzzer
structurally cannot. A classic case: a username containing ``' OR 1=1--`` is
accepted by ``POST /register`` (stored intact, 200), and later a clean
``GET /users`` that lists it crashes while building its query. The crash is on a
request that carried no payload of its own, so the attack and the symptom are
two different operations.

Detection is correlation-based, so it is reported with care:

* a **clean** read (no injection payload of its own) returns 5XX, *and*
* a payload was earlier accepted into a field of a resource this read touches,

then it is flagged -- as a ``violation`` when the stored payload string actually
appears in the error body (the crash is demonstrably about our data), otherwise
as a ``warning`` (the timing is suggestive but not proof). The taint record is
maintained by the runner; this oracle only reads it.
"""
from __future__ import annotations

from typing import Any

from ..engine.pool import norm, resource_of, singular
from ..models import OracleViolation, Sequence
from .base import Oracle, OracleContext

# payloads only count as "stored" if they are recognisably hostile, so an
# ordinary value that merely happens to reappear is not treated as taint
INJECTION_MARKERS = ("'", '"', "--", "/*", "<", ">", "${", "{{", "$where", "$ne",
                     "sleep", "union", "select", "drop", "or 1=1", "../")


def looks_injected(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 500:
        return False
    low = value.lower()
    return any(m in low for m in INJECTION_MARKERS)


def record_taint(taint: dict, op, req, resp) -> None:
    """Note any hostile string a write just stored, keyed by resource type.

    Called by the runner after each response. Only successful writes count: a
    payload that was rejected was never stored and cannot resurface later.
    """
    if resp is None or not resp.ok or req.method.upper() not in ("POST", "PUT", "PATCH"):
        return
    body = req.body if isinstance(req.body, dict) else None
    if not body:
        return
    rtype = resource_of(req.path)
    if not rtype:
        return
    for v in body.values():
        if looks_injected(v):
            taint.setdefault(rtype, set()).add(v)


class SecondOrderInjectionOracle(Oracle):
    """A clean read that crashes after a payload was stored in what it reads."""
    name = "second_order_injection"
    active = False        # purely inspects what already happened
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if not ctx.taint:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            # the read must be clean -- a request carrying its own payload that
            # 5XXs is ordinary (first-order) injection, already covered elsewhere
            if resp is None or not resp.is_5xx or req.mutation:
                continue
            rtype = resource_of(req.path)
            stored = ctx.taint.get(rtype) or ctx.taint.get(norm(singular(rtype or "")))
            if not stored:
                continue
            body_text = (resp.text or "")[:4000]
            echoed = next((p for p in stored if p in body_text), None)
            if echoed is not None:
                out.append(self.violation(
                    req.op_key,
                    f"a clean {req.method} on {rtype} crashed ({resp.status}) and the "
                    f"error echoes a payload stored earlier: {echoed[:50]!r}",
                    severity="violation", resource=rtype, payload=echoed[:120]))
            else:
                out.append(self.violation(
                    req.op_key,
                    f"a clean {req.method} on {rtype} crashed ({resp.status}) after a "
                    f"payload was stored in that resource -- possible second-order injection",
                    severity="warning", resource=rtype,
                    stored_samples=sorted(stored)[:3]))
        return out
