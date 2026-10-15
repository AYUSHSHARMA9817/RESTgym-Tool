"""CRUD roundtrip and zombie-resource oracles.

Milestone 3 of docs/architecture/track_a_stateful_fuzzing.md §4.5 (oracles 2
and 3). Both are *active*: they send a follow-up GET of their own.

Tolerances (per the doc's warning block), because both checks are otherwise
false-positive machines:

* **Roundtrip** compares only the fields the request body actually sent, minus
  anything ``readOnly`` or volatile. A server is entitled to normalise,
  default, compute and timestamp whatever the client did not set.
* **Zombie** re-reads a few times with backoff before flagging, since an API
  with asynchronous deletion or a read replica may legitimately still serve the
  entity for a moment after a 2XX DELETE.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from ..engine.pool import norm, resource_of
from ..models import Operation, OracleViolation, Request, Sequence, Step
from .base import (Oracle, OracleContext, is_volatile, readonly_fields, state_diff,
                   values_equivalent)

_PARAM_RE = re.compile(r"\{([^}]+)\}")
_GONE = (404, 410)


def find_item_get(ctx: OracleContext, op: Operation) -> Optional[Operation]:
    """The GET that reads back a single entity touched by ``op``.

    For a collection create (``POST /owners``) that is ``GET /owners/{x}``; for
    an item-level operation (``PUT|DELETE /owners/{x}``) it is the GET on that
    same path.
    """
    path = op.path.rstrip("/")
    if _PARAM_RE.search(path.rsplit("/", 1)[-1]):          # already an item path
        want = path
        for cand in ctx.ops.values():
            if cand.method == "GET" and cand.path.rstrip("/") == want:
                return cand
        return None
    pattern = re.compile(re.escape(path) + r"/\{[^/{}]+\}$")
    for cand in ctx.ops.values():
        if cand.method == "GET" and pattern.match(cand.path.rstrip("/")):
            return cand
    return None


def bind_item_path(get_op: Operation, source: Request, entity_id: Any) -> Optional[Request]:
    """Build a GET request for one entity, reusing the source request's ancestors."""
    params = _PARAM_RE.findall(get_op.path)
    if not params:
        return None
    path_params = {p: source.path_params[p] for p in params if p in source.path_params}
    path_params[params[-1]] = entity_id        # the last parameter names the entity itself
    if len(path_params) != len(params):
        return None                            # an ancestor id we don't have; skip the check
    return Request(method="GET", path=get_op.path, path_params=path_params, op_key=get_op.key)


def _identity_fields(op: Operation) -> set[str]:
    """Normalised body field names that name the entity itself, not its data.

    On an item-level route the path parameter decides which entity is being
    written -- ``PUT /pettypes/659`` updates 659 whatever the body's ``id``
    says, and a server that ignores the body's id is behaving correctly. Left
    in the comparison this produced a false "written field not persisted --
    id: sent 283, read back 659" against a live pet-clinic, so identity fields
    are excluded from the round-trip check. Their *data* fields still count.
    """
    path = op.path.rstrip("/")
    if not _PARAM_RE.search(path.rsplit("/", 1)[-1]):
        return set()                     # collection route: the body's id is meaningful
    out = {"id", "_id", "uuid"}
    res = resource_of(op.path)
    if res:
        out.add(norm(res + "id"))
    for name in _PARAM_RE.findall(op.path):
        out.add(norm(name))
    return out


def _written_fields(req: Request) -> dict:
    """Scalar fields the client actually set in the request body."""
    body = req.body if isinstance(req.body, dict) else (req.form if isinstance(req.form, dict) else {})
    return {k: v for k, v in (body or {}).items()
            if not isinstance(v, (dict, list)) and not is_volatile(k)}


class CrudRoundtripOracle(Oracle):
    """A field the server accepted on create/update must come back on read."""
    name = "crud_roundtrip"
    active = True

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or not resp.ok or req.method not in ("POST", "PUT", "PATCH"):
                continue
            if req.mutation:
                continue        # a deliberately corrupted request proves nothing about persistence
            op = ctx.op(req.op_key)
            if op is None:
                continue
            sent = _written_fields(req)
            if not sent:
                continue
            entity_id = _entity_id(resp, req)
            if entity_id is None:
                continue
            get_op = find_item_get(ctx, op)
            if get_op is None:
                continue
            probe = bind_item_path(get_op, req, entity_id)
            if probe is None:
                continue
            got = await ctx.client.send(probe)
            if not got.ok or not isinstance(got.json, dict):
                continue
            ro = readonly_fields(op.request_body.schema if op.request_body else None)
            identity = _identity_fields(op)
            missing, wrong = [], []
            for k, v in sent.items():
                if k in ro or norm(k) in identity:
                    continue
                if k not in got.json:
                    missing.append(k)
                elif not values_equivalent(v, got.json[k]):
                    wrong.append(f"{k}: sent {v!r}, read back {got.json[k]!r}")
            if wrong:
                out.append(self.violation(
                    req.op_key, f"written field not persisted -- {wrong[0]}",
                    severity="violation", entity_id=entity_id, mismatches=wrong[:5],
                    read_back_from=get_op.key))
            elif missing:
                out.append(self.violation(
                    req.op_key, f"written field absent on read back: {', '.join(missing[:3])}",
                    severity="warning", entity_id=entity_id, missing=missing[:5],
                    read_back_from=get_op.key))
        return out


class ZombieResourceOracle(Oracle):
    """After a successful DELETE, the entity must stop being readable."""
    name = "zombie_resource"
    active = True

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp, req = step.response, step.request
            if resp is None or req.method != "DELETE" or not resp.ok:
                continue
            op = ctx.op(req.op_key)
            if op is None:
                continue
            params = _PARAM_RE.findall(op.path)
            if not params or params[-1] not in req.path_params:
                continue
            entity_id = req.path_params[params[-1]]
            get_op = find_item_get(ctx, op)
            if get_op is None:
                continue
            probe = bind_item_path(get_op, req, entity_id)
            if probe is None:
                continue

            # bounded re-reads: tolerate asynchronous deletion / replica lag
            delay = ctx.zombie_backoff
            last = None
            for attempt in range(max(1, ctx.zombie_retries)):
                last = await ctx.client.send(probe)
                if last.status in _GONE or last.status == 0 or last.status >= 500:
                    break
                if attempt + 1 < ctx.zombie_retries:
                    await asyncio.sleep(delay)
                    delay *= 2
            if last is not None and last.ok:
                out.append(self.violation(
                    req.op_key,
                    f"entity still readable after a {resp.status} DELETE "
                    f"({ctx.zombie_retries} re-reads, still {last.status})",
                    severity="violation", entity_id=entity_id,
                    delete_status=resp.status, read_status=last.status,
                    read_back_from=get_op.key))
        return out


def _entity_id(resp, req: Request) -> Any:
    if isinstance(resp.json, dict):
        for k in ("id", "Id", "ID", "_id", "uuid"):
            v = resp.json.get(k)
            if v is not None and not isinstance(v, (dict, list)):
                return v
    loc = resp.headers.get("location") or resp.headers.get("Location")
    if loc:
        last = loc.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        if last:
            return int(last) if last.isdigit() else last
    params = _PARAM_RE.findall(req.path)
    if params and params[-1] in req.path_params:
        return req.path_params[params[-1]]
    return None
