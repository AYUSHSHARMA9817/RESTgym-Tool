"""Check BFLA (C3) and the generalised mass-assignment read-back (D4).

Run: python3 -m tests.check_bfla_d4
"""
import asyncio

from restleague.models import Operation, Parameter, Request, RequestBody, Response, Sequence, Step
from restleague.oracles import BrokenFunctionAuthOracle, MassAssignmentOracle, OracleContext

# ---------------------------------------------------------- BFLA (C3)
debug = Operation("GET", "/users/v1/_debug")          # admin-ish by name
normal = Operation("GET", "/users/v1/{username}",
                   parameters=[Parameter("username", "path", True, {})])
OPS = {o.key: o for o in (debug, normal)}


class Client:
    def __init__(self, resp): self.resp, self.sent = resp, []
    async def send(self, req): self.sent.append(req); return self.resp


def one(req, resp):
    s = Sequence(target_op_key=req.op_key); s.append(Step(req, resp)); return s


def run(seq, **kw):
    return asyncio.run(BrokenFunctionAuthOracle().check(seq, OracleContext(ops=OPS, **kw)))


dbg_req = Request("GET", "/users/v1/_debug", op_key=debug.key)
ok = Response(status=200, json={"users": []})

# admin function answered to an anonymous client -> violation
v = run(one(dbg_req, ok), unauth_client=Client(Response(status=200, json={})))
assert v and v[0].severity == "violation" and "no credentials" in v[0].summary, v
print("OK   BFLA flags an admin function reachable with no credentials")

# admin function answered to a second plain user -> violation
v = run(one(dbg_req, ok), unauth_client=Client(Response(status=401)),
        alt_client=Client(Response(status=200, json={})))
assert v and "non-privileged user" in v[0].summary, v
print("OK   BFLA flags an admin function reachable by another plain user")

# properly gated: both low-priv principals refused -> silent
assert run(one(dbg_req, ok), unauth_client=Client(Response(status=401)),
           alt_client=Client(Response(status=403))) == []
print("OK   BFLA stays silent when the admin function is properly gated")

# a non-admin operation is never a BFLA finding, even if anon succeeds
norm_req = Request("GET", "/users/v1/{username}", path_params={"username": "a"}, op_key=normal.key)
assert run(one(norm_req, ok), unauth_client=Client(Response(status=200, json={}))) == []
print("OK   BFLA ignores operations that are not administrative by name")


# ----------------------------------------- generalised mass-assignment (D4)
# register declares only username/password/email; admin is undeclared
register = Operation("POST", "/users/v1/register", request_body=RequestBody(
    schema={"type": "object", "properties": {"username": {}, "password": {}, "email": {}}}))
item_get = Operation("GET", "/users/v1/{username}",
                     parameters=[Parameter("username", "path", True, {})])
debug_get = Operation("GET", "/users/v1/_debug")
MA_OPS = {o.key: o for o in (register, item_get, debug_get)}


class VampiLike:
    """register accepts+stores admin but only _debug exposes it (like VAmPI)."""
    def __init__(self):
        self.users = {}
        self.sent = []

    async def send(self, req):
        self.sent.append(req)
        if req.method == "POST":
            b = dict(req.body or {})
            self.users[b.get("username")] = b           # stores everything, incl. admin
            return Response(status=200, json={"message": "registered"})   # echoes nothing
        if req.path == "/users/v1/_debug":
            return Response(status=200, json={"users": list(self.users.values())})
        if "{username}" in req.path:                    # item GET hides admin
            u = self.users.get(req.path_params.get("username"), {})
            return Response(status=200, json={"username": u.get("username"), "email": u.get("email")})
        return Response(status=404, json={})


async def d4():
    api = VampiLike()
    req = Request("POST", "/users/v1/register",
                  body={"username": "hacker", "password": "p", "email": "h@x.com"},
                  op_key=register.key)
    seq = Sequence(); seq.append(Step(req, Response(status=200, json={"message": "ok"})))
    v = await MassAssignmentOracle().check(seq, OracleContext(ops=MA_OPS, client=api))
    assert v, "admin:true stored but hidden from item GET should still be caught via _debug"
    assert "admin" in v[0].evidence.get("fields", []), v[0].evidence
    assert v[0].evidence.get("read_back") == "GET /users/v1/_debug", v[0].evidence
    print("OK   mass-assignment confirms a hidden field via a debug/list endpoint:",
          v[0].evidence["fields"])

    # and it must NOT fire when nothing stuck anywhere
    class Clean(VampiLike):
        async def send(self, req):
            self.sent.append(req)
            if req.method == "POST":
                b = {k: v for k, v in (req.body or {}).items()
                     if k in ("username", "password", "email")}
                self.users[b.get("username")] = b       # drops unknown fields
                return Response(status=200, json={"message": "ok"})
            return await super().send(req)
    api2 = Clean()
    v2 = await MassAssignmentOracle().check(seq, OracleContext(ops=MA_OPS, client=api2))
    assert v2 == [], v2
    print("OK   mass-assignment stays silent when unknown fields are dropped")


asyncio.run(d4())
print("ALL CHECKS PASSED")
