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
import re
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
_PARAM_RE = re.compile(r"\{([^}]+)\}")


def _objects(body: Any):
    """Yield every dict in a response, whether it is one object or a list/wrapper."""
    if isinstance(body, dict):
        yield body
        for v in body.values():
            if isinstance(v, list):
                for x in v:
                    if isinstance(x, dict):
                        yield x
    elif isinstance(body, list):
        for x in body:
            if isinstance(x, dict):
                yield x


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
        entity_id = _entity_id(written, probe)
        if get_op is not None and entity_id is not None:
            read = bind_item_path(get_op, probe, entity_id)
            if read is not None:
                got = await ctx.client.send(read)
                if got.ok and isinstance(got.json, dict):
                    hit = [k for k, v in extras.items()
                           if k in got.json and values_equivalent(v, got.json[k])]
                    if hit:
                        self._source = get_op.key
                        return hit

        # The item GET may be absent or may not echo the field even though it was
        # stored -- VAmPI
        # keeps `admin` but only exposes it via /_debug. So also scan listing and
        # debug endpoints for an object that carries our value, matched to our
        # entity by an identifying field we sent, so a coincidental match on
        # someone else's record is not mistaken for ours.
        return await self._read_back_from_listings(ctx, op, req_body=probe.body, extras=extras)

    async def _read_back_from_listings(self, ctx: OracleContext, op, req_body, extras) -> list:
        ident = {k: v for k, v in (req_body or {}).items()
                 if not isinstance(v, (dict, list)) and k not in extras}
        for cand in self._listing_ops(ctx, op):
            resp = await ctx.client.send(Request(method="GET", path=cand.path,
                                                 op_key=cand.key))
            if not resp.ok:
                continue
            for obj in _objects(resp.json):
                if ident and not all(k in obj and values_equivalent(v, obj[k])
                                     for k, v in ident.items()):
                    continue             # not our entity
                hit = [k for k, v in extras.items()
                       if k in obj and values_equivalent(v, obj[k])]
                if hit:
                    self._source = cand.key
                    return hit
        return []

    @staticmethod
    def _listing_ops(ctx: OracleContext, op):
        """GET ops that return a set of this resource: its collection, plus any
        debug/dump route that might expose fields the item view hides."""
        from ..engine.pool import resource_of
        rtype = resource_of(op.path)
        out = []
        for cand in ctx.ops.values():
            if cand.method != "GET" or _PARAM_RE.search(cand.path.rstrip("/").rsplit("/", 1)[-1]):
                continue                 # item routes handled above
            name = cand.path.lower()
            if resource_of(cand.path) == rtype or any(t in name for t in ("debug", "dump", "all")):
                out.append(cand)
        return out
