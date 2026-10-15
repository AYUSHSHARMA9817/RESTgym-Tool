"""Mass-assignment oracle: does the API accept fields it never advertised?

Frameworks that bind a request body straight onto a model object (Rails'
``update_attributes``, Spring's data binding, Mongoose, Django serializers with
``fields = '__all__'``) will happily write any field the client sends, whether
or not it appears in the API's contract. Sending ``{"isAdmin": true}`` alongside
a legitimate body is then enough to escalate privilege. It is one of the most
common real-world REST bugs (OWASP API6, "Mass Assignment") and a single-request
fuzzer never finds it, because finding it requires writing a field and then
*reading the entity back* to see whether it stuck.

The oracle only flags a field when all three hold:

1. the OpenAPI request body does **not** declare it -- so the API never asked
   for it and has no reason to store it;
2. the write with the extra field succeeded;
3. reading the entity back returns **our** value for it.

Point 3 is what separates a real finding from noise: an API that quietly drops
unknown fields, or that happens to have its own unrelated ``role`` field, will
not return the value we sent.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from ..models import OracleViolation, Request, Sequence
from .base import Oracle, OracleContext, values_equivalent
from .crud import _entity_id, bind_item_path, find_item_get

#: Fields worth trying: privilege, ownership and money. Values are chosen to be
#: unmistakable if they come back, and harmless if the API ignores them.
PROBE_FIELDS: dict[str, Any] = {
    "isAdmin": True,
    "admin": True,
    "role": "superadmin",
    "roles": ["superadmin"],
    "isActive": True,
    "enabled": True,
    "verified": True,
    "emailVerified": True,
    "isDeleted": False,
    "balance": 999999,
    "credit": 999999,
    "discount": 100,
    "price": 0,
    "ownerId": 999999,
    "userId": 999999,
    "tenantId": 999999,
}
MAX_PROBES_PER_REQUEST = 6


def _declared_fields(op) -> set[str]:
    """Property names the operation's request body actually advertises."""
    rb = getattr(op, "request_body", None)
    if rb is None or not isinstance(rb.schema, dict):
        return set()
    return set((rb.schema.get("properties") or {}).keys())


class MassAssignmentOracle(Oracle):
    """Undeclared privileged fields must not be persisted."""
    name = "mass_assignment"
    active = True
    mutating = True        # its probe is a real write

    def __init__(self) -> None:
        self._source = ""   # where the read-back evidence came from

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or not resp.ok or req.mutation:
                continue
            if req.method.upper() not in ("POST", "PUT", "PATCH"):
                continue
            if not isinstance(req.body, dict):
                continue            # only JSON object bodies can carry extra fields
            op = ctx.op(req.op_key)
            if op is None:
                continue

            declared = _declared_fields(op)
            extras = {k: v for k, v in PROBE_FIELDS.items() if k not in declared}
            if not extras:
                continue
            extras = dict(list(extras.items())[:MAX_PROBES_PER_REQUEST])

            probe = copy.deepcopy(req)
            probe.mutation = "mass_assignment"
            probe.body = {**req.body, **extras}
            written = await ctx.client.send(probe)
            if not written.ok:
                continue            # correct: the API rejected the unknown fields

            stuck = await self._read_back(ctx, op, probe, written, extras)
            if stuck:
                names = ", ".join(f"{k}={extras[k]!r}" for k in stuck)
                out.append(self.violation(
                    req.op_key,
                    f"undeclared field(s) persisted: {names}",
                    severity="violation", accepted_status=written.status,
                    fields=stuck, sent=extras, read_back=self._source))
        return out

    async def _read_back(self, ctx: OracleContext, op, probe: Request, written,
                         extras: dict) -> list:
        """Which of the extra fields come back carrying the value we sent?

        The write's own response is preferred when it echoes the entity, since
        that costs no extra request; otherwise the entity is fetched through
        its item GET. An empty list means nothing stuck -- the normal, correct
        outcome.
        """
        self._source = "write response"
        body = written.json if isinstance(written.json, dict) else None
        if body is not None and any(k in body for k in extras):
            return [k for k, v in extras.items()
                    if k in body and values_equivalent(v, body[k])]

        get_op = find_item_get(ctx, op)
        if get_op is None:
            return []
        entity_id = _entity_id(written, probe)
        if entity_id is None:
            return []
        read = bind_item_path(get_op, probe, entity_id)
        if read is None:
            return []
        got = await ctx.client.send(read)
        if not got.ok or not isinstance(got.json, dict):
            return []
        self._source = get_op.key
        return [k for k, v in extras.items()
                if k in got.json and values_equivalent(v, got.json[k])]
