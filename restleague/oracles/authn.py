"""Authorization-enforcement oracles: is auth actually required and isolated?

Two bugs from the taxonomy's section C, both needing extra clients the normal
run does not use:

* **MissingAuthOracle (C4)** -- an operation the spec marks as requiring auth
  that answers a request with no credentials. Sound because it keys on the
  spec's own ``security`` requirement: a 2XX without a token is only flagged
  when the operation *declares* that a token is needed. A deliberately public
  endpoint (``security: []``) is never flagged.

* **CrossUserAccessOracle (C2, BOLA across users)** -- a request that user A was
  allowed to make, which user B is *also* allowed to make against the same
  resource. Replays A's successful request with B's credentials. A write that
  succeeds for B is a violation (B changed A's resource); a read that returns
  the same body A saw is a violation (B read A's data); any other 2XX is a
  warning, since the resource might legitimately be shared.

Both are active (they send their own requests) and mutating where they replay a
write, so they run only on the final step and are stood down under concurrency,
like the other active oracles.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from ..models import OracleViolation, Request, Sequence
from .base import Oracle, OracleContext, stable_view

_PARAM_RE = re.compile(r"\{([^}]+)\}")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# path/operation tokens that mark a function as administrative. A black-box tool
# cannot read the server's role model, so this naming heuristic is how it asserts
# "this function should require elevated privilege" -- the premise BFLA needs.
# "/all" was here but is too weak a signal: a self-scoped listing like
# ``GET /shop/orders/all`` means "all of *my* orders", not an administrative
# function, and a second user getting 200 on their own (empty) list is not a
# privilege escalation. It produced a false BFLA against crAPI's orders/all.
# The remaining tokens denote genuinely elevated functions; a real admin list
# endpoint (crAPI's ``/management/users/all``) still matches via "manage".
ADMIN_TOKENS = ("admin", "debug", "internal", "manage", "moderat", "superuser",
                "sudo", "root", "backend", "config", "dump")


def _is_admin_op(op) -> bool:
    hay = (op.path + " " + (op.operation_id or "")).lower()
    return any(t in hay for t in ADMIN_TOKENS)


def _requires_auth(op) -> bool:
    """Does the spec say this operation needs credentials?"""
    return bool(op and op.security)


def _strip_auth(req: Request) -> Request:
    """A copy with any Authorization-ish header removed, so the client's auth
    provider is the only thing that could add one back (and the unauth client
    has none)."""
    clone = copy.deepcopy(req)
    for h in list(clone.headers):
        if h.lower() in ("authorization", "cookie", "x-auth-token", "x-access-token"):
            clone.headers.pop(h, None)
    clone.mutation = None
    return clone


class MissingAuthOracle(Oracle):
    """A protected operation must not answer an unauthenticated request."""
    name = "missing_auth"
    active = True
    mutating = True        # may replay a write without credentials

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.unauth_client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or not resp.ok or req.mutation:
                continue
            op = ctx.op(req.op_key)
            if not _requires_auth(op):
                continue                 # spec says this endpoint is public; not a bug
            again = await ctx.unauth_client.send(_strip_auth(req))
            if again.ok:
                out.append(self.violation(
                    req.op_key,
                    f"{req.method} succeeded ({again.status}) with no credentials, "
                    f"though the spec requires auth",
                    severity="violation", authed_status=resp.status,
                    unauth_status=again.status))
        return out


class CrossUserAccessOracle(Oracle):
    """One user's authorized request must not succeed for a different user."""
    name = "cross_user_access"
    active = True
    mutating = True        # replays writes as the other user

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.alt_client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or not resp.ok or req.mutation:
                continue
            op = ctx.op(req.op_key)
            if not _requires_auth(op):
                continue                 # public endpoint: cross-user access is expected
            # must address a specific resource, or "another user's" has no meaning
            if not _PARAM_RE.search(req.path):
                continue

            as_b = await ctx.alt_client.send(_strip_auth(req))
            if not as_b.ok:
                continue                 # correct: B is refused (401/403/404)

            is_write = req.method.upper() in WRITE_METHODS
            if is_write:
                out.append(self.violation(
                    req.op_key,
                    f"a second user's token was accepted ({as_b.status}) for "
                    f"{req.method} on another user's resource",
                    severity="violation", method=req.method,
                    user_a_status=resp.status, user_b_status=as_b.status))
            elif resp.json is not None and stable_view(as_b.json) == stable_view(resp.json):
                out.append(self.violation(
                    req.op_key,
                    "a second user read back the exact data the first user saw "
                    "from a protected resource",
                    severity="violation", method=req.method,
                    user_b_status=as_b.status))
            else:
                out.append(self.violation(
                    req.op_key,
                    f"a protected {req.method} returned {as_b.status} for a second "
                    f"user; the resource may not be owner-isolated",
                    severity="warning", method=req.method,
                    user_b_status=as_b.status))
        return out


class BrokenFunctionAuthOracle(Oracle):
    """An administrative function reachable by a low-privilege principal (BFLA).

    The premise -- "this function is administrative" -- comes from the naming
    heuristic (``_is_admin_op``), since a black-box tool cannot read the role
    model. Given a successful request on such a function, it replays with a
    principal that should *not* be allowed: no credentials, and a second plain
    user. A 2XX from either means the privileged function is not function-gated.

    This is distinct from missing_auth (C4): that fires only when the spec
    *declares* the op needs auth. An intentionally-public debug endpoint that
    dumps every password declares no auth, so C4 stays silent -- but the
    function is clearly privileged, which is exactly what this oracle asserts.
    """
    name = "broken_function_auth"
    active = True
    mutating = True        # replays whatever the op was, possibly a write

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        if ctx.unauth_client is None and ctx.alt_client is None:
            return []
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or not resp.ok or req.mutation:
                continue
            op = ctx.op(req.op_key)
            if not op or not _is_admin_op(op):
                continue

            if ctx.unauth_client is not None:
                anon = await ctx.unauth_client.send(_strip_auth(req))
                if anon.ok:
                    out.append(self.violation(
                        req.op_key,
                        f"administrative function answered {req.method} with no "
                        f"credentials ({anon.status})",
                        severity="violation", principal="anonymous",
                        status=anon.status))
                    continue
            if ctx.alt_client is not None:
                asb = await ctx.alt_client.send(_strip_auth(req))
                if asb.ok:
                    out.append(self.violation(
                        req.op_key,
                        f"administrative function answered {req.method} for a second, "
                        f"non-privileged user ({asb.status})",
                        severity="violation", principal="other-user",
                        status=asb.status))
        return out
