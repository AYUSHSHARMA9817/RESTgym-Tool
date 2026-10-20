"""Authorization oracles: did the API serve data belonging to someone else?

The sequence mutator's ``cross_entity`` operator already builds the attack --
it binds a nested path to a child that belongs to a *different* parent, e.g.
``/owners/1/pets/20`` where pet 20 belongs to owner 2. Until this oracle
existed the attack was sent and the answer thrown away, so the most valuable
class of bug the tool can produce went unnoticed.

The rule: a request whose path claims parent A while addressing a child of
parent B must not succeed. A correct API answers 404 (it does not exist *under
that parent*) or 403. A 2XX means the parent segment is not being enforced --
in a multi-tenant system that is Broken Object Level Authorization (BOLA,
OWASP API1), and even in a single-tenant one it is a routing bug that lets
clients reach records through paths that imply ownership they do not have.

Confidence is graded rather than assumed:

* the response body carries the foreign child's id  -> ``violation``; the API
  demonstrably served another parent's record
* a write method succeeded                          -> ``violation``; worse
  than a read, since it modified another parent's record
* some other 2XX                                    -> ``warning``; the parent
  segment looks unenforced but the body does not prove what was served
"""
from __future__ import annotations

import re
from typing import Any

from ..models import OracleViolation, Sequence
from .base import Oracle, OracleContext

_PARAM_RE = re.compile(r"\{([^}]+)\}")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _body_mentions(body: Any, value: Any, depth: int = 0) -> bool:
    """Does the response body contain this id anywhere (as a value)?"""
    if depth > 5:
        return False
    if isinstance(body, dict):
        for v in body.values():
            if v == value or (not isinstance(v, (dict, list)) and str(v) == str(value)):
                return True
            if isinstance(v, (dict, list)) and _body_mentions(v, value, depth + 1):
                return True
        return False
    if isinstance(body, list):
        return any(_body_mentions(v, value, depth + 1) for v in body[:20])
    return False


class CrossEntityAccessOracle(Oracle):
    """A child addressed under the wrong parent must not be served."""
    name = "cross_entity_access"
    active = False        # passive: it judges a request the executor already sent
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or req.mutation != "cross_entity":
                continue
            if not resp.ok:
                continue          # 403/404/4xx: the API enforced the relationship

            params = _PARAM_RE.findall(req.path)
            if len(params) < 2:
                continue
            parent_name, child_name = params[-2], params[-1]
            parent_id = req.path_params.get(parent_name)
            child_id = req.path_params.get(child_name)

            served = _body_mentions(resp.json, child_id) if resp.json is not None else False
            is_write = req.method.upper() in WRITE_METHODS
            if served:
                detail = "returned the foreign record"
            elif is_write:
                detail = "accepted a write to the foreign record"
            else:
                detail = "did not reject the request"

            out.append(self.violation(
                req.op_key,
                f"{resp.status} for {child_name}={child_id!r} under {parent_name}={parent_id!r} "
                f"-- it belongs to a different parent; the API {detail}",
                severity="violation" if (served or is_write) else "warning",
                status=resp.status, method=req.method,
                parent={parent_name: parent_id}, foreign_child={child_name: child_id},
                body_excerpt=(resp.text or "")[:200]))
        return out
