"""Lifecycle / integrity oracles: race conditions and dangling references.

* **RaceConditionOracle (D7)** -- judges the pair of concurrent requests the
  ``double_action`` sequence mutation fires. The soundest signal is a DELETE
  whose two in-flight copies *both* succeed: a correctly serialised API answers
  one 204 and one 404, so two 204s mean the resource was deleted twice without
  locking. Other non-idempotent operations that both succeed are a warning,
  since for them two successes can be legitimate.

* **DanglingReferenceOracle (D6)** -- after a parent is deleted, a child that
  outlives it is a referential-integrity bug. The graph already records which
  entities were orphaned by a cascade delete; this reads each orphan back
  through its own id route and flags a 2XX. Only a child with a *standalone*
  route (reachable without the now-deleted parent in the path) can be tested;
  a child only reachable under the parent would 404 for the right reason.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..engine.pool import norm, resource_of, singular
from ..models import Operation, OracleViolation, Request, Sequence
from .base import Oracle, OracleContext

_PARAM_RE = re.compile(r"\{([^}]+)\}")
NON_IDEMPOTENT = ("POST", "PUT", "PATCH", "DELETE")


class RaceConditionOracle(Oracle):
    """Two concurrent copies of a one-shot action must not both take effect."""
    name = "race_condition"
    active = False        # the executor already fired both requests
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            # the twin step carries the first copy's status; judge the pair once
            twin_status = step.bindings.get("_twin_status")
            if req.mutation != "double_action" or twin_status is None or resp is None:
                continue
            both_ok = (200 <= resp.status < 300) and (200 <= twin_status < 300)
            if not both_ok:
                continue                 # one was rejected -> the API serialised, fine
            method = req.method.upper()
            # POST to a login/token/auth endpoint is not a "create once" action,
            # so two concurrent successes are expected, not a duplicate-create.
            is_auth_action = any(t in req.path.lower()
                                 for t in ("login", "logout", "signin", "signout",
                                           "token", "session", "authenticate", "refresh"))
            is_collection_post = (method == "POST" and not is_auth_action
                                  and not _PARAM_RE.search(req.path.rstrip("/").rsplit("/", 1)[-1]))
            if method == "DELETE":
                out.append(self.violation(
                    req.op_key,
                    f"two concurrent DELETEs of the same resource both succeeded "
                    f"({resp.status} and {twin_status}); the second should have 404'd",
                    severity="violation", statuses=[resp.status, twin_status]))
            elif is_collection_post:
                # two concurrent creates from one logical action -> possible
                # duplicate-submit / double-spend. PUT and PATCH are excluded
                # on purpose: they are idempotent by definition, so both copies
                # succeeding is correct, not a bug (this was noise on pet-clinic).
                out.append(self.violation(
                    req.op_key,
                    f"two concurrent creates both succeeded ({resp.status} and "
                    f"{twin_status}); one action may have created two resources",
                    severity="warning", statuses=[resp.status, twin_status]))
        return out


def _standalone_get(ctx: OracleContext, child_type: str) -> Optional[Operation]:
    """A GET that fetches one `child_type` by id, preferring the route with the
    fewest path parameters (so it does not need the deleted parent)."""
    best = None
    for op in ctx.ops.values():
        if op.method != "GET":
            continue
        path = op.path.rstrip("/")
        last = path.rsplit("/", 1)[-1]
        if not _PARAM_RE.fullmatch(last):
            continue                     # not an item route
        if resource_of(op.path) != child_type:
            continue
        if best is None or op.path.count("{") < best.path.count("{"):
            best = op
    return best


class DanglingReferenceOracle(Oracle):
    """A child must not remain readable once its parent is deleted."""
    name = "dangling_reference"
    active = True         # reads each orphaned child back
    mutating = False      # GET only

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.client is None or ctx.rkg is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or req.method.upper() != "DELETE" or not resp.ok or req.mutation:
                continue
            params = _PARAM_RE.findall(req.path)
            if not params or params[-1] not in req.path_params:
                continue
            parent_type = resource_of(req.path)
            parent_id = req.path_params[params[-1]]
            if not parent_type:
                continue
            try:
                children = ctx.rkg.children_of(parent_type, parent_id)
            except Exception:
                continue
            seen: set = set()
            for child in children:
                get_op = _standalone_get(ctx, child.entity_type)
                if get_op is None:
                    continue
                gp = _PARAM_RE.findall(get_op.path)
                if len(gp) != 1:         # needs the (gone) parent too -> inconclusive
                    continue
                key = (get_op.key, child.entity_id)
                if key in seen:
                    continue
                seen.add(key)
                probe = Request(method="GET", path=get_op.path,
                                path_params={gp[0]: child.entity_id}, op_key=get_op.key)
                got = await ctx.client.send(probe)
                if got.ok:
                    out.append(self.violation(
                        req.op_key,
                        f"{child.entity_type} {child.entity_id} is still readable via "
                        f"{get_op.key} after its parent {parent_type} {parent_id} was deleted",
                        severity="violation", orphan={child.entity_type: child.entity_id},
                        parent={parent_type: parent_id}, read_back_from=get_op.key))
        return out
