"""Check the authorization-enforcement oracles: missing-auth and cross-user (BOLA).

Run: python3 -m tests.check_authn_oracles
"""
import asyncio

from restleague.models import Operation, Parameter, Request, Response, Sequence, Step
from restleague.oracles import CrossUserAccessOracle, MissingAuthOracle, OracleContext

OWNER = Parameter("username", "path", True, {"type": "string"})
# a protected item-level op (spec says it needs a token) and a public one
protected = Operation("PUT", "/users/v1/{username}/email", parameters=[OWNER],
                      security=[{"bearerAuth": []}])
protected_get = Operation("GET", "/users/v1/{username}", parameters=[OWNER],
                          security=[{"bearerAuth": []}])
public = Operation("GET", "/users/v1/{username}", parameters=[OWNER], security=[])
OPS = {o.key: o for o in (protected, protected_get)}
PUB = {public.key: public}


class Client:
    """Returns a fixed response; records what it was asked to send."""
    def __init__(self, resp):
        self.resp, self.sent = resp, []

    async def send(self, req):
        self.sent.append(req)
        return self.resp


def one(req, resp, target=None):
    s = Sequence(target_op_key=target or req.op_key)
    s.append(Step(req, resp))
    return s


def run(oracle, seq, **ctx_kw):
    return asyncio.run(oracle.check(seq, OracleContext(ops=OPS, **ctx_kw)))


# ------------------------------------------------------- missing auth (C4)
authed = Request("PUT", "/users/v1/{username}/email", path_params={"username": "name1"},
                 headers={"Authorization": "Bearer A"}, body={"email": "a@x.com"},
                 op_key=protected.key)
ok = Response(status=200, json={"status": "success"})

# protected op answers with no credentials -> violation
v = run(MissingAuthOracle(), one(authed, ok), unauth_client=Client(Response(status=200)))
assert v and v[0].severity == "violation" and "no credentials" in v[0].summary, v
print("OK   missing-auth flags a protected op that answers unauthenticated")

# protected op correctly refuses the unauthenticated replay -> silent
assert run(MissingAuthOracle(), one(authed, ok),
           unauth_client=Client(Response(status=401))) == []
print("OK   missing-auth stays silent when the op returns 401 without a token")

# a public op (security: []) is never flagged, even if it answers unauth
pub_req = Request("GET", "/users/v1/{username}", path_params={"username": "name1"},
                  op_key=public.key)
assert asyncio.run(MissingAuthOracle().check(
    one(pub_req, ok), OracleContext(ops=PUB, unauth_client=Client(Response(status=200))))) == []
print("OK   missing-auth never flags a deliberately public endpoint")

# no unauth client -> oracle is a no-op
assert run(MissingAuthOracle(), one(authed, ok)) == []
print("OK   missing-auth skips when no unauthenticated client is available")


# ------------------------------------------------- cross-user / BOLA (C2)
# user B's token accepted for a WRITE on user A's resource -> violation
v = run(CrossUserAccessOracle(), one(authed, ok),
        alt_client=Client(Response(status=200, json={"status": "success"})))
assert v and v[0].severity == "violation" and "second user" in v[0].summary, v
print("OK   cross-user flags B's token writing A's resource")

# B refused -> correct, silent
assert run(CrossUserAccessOracle(), one(authed, ok),
           alt_client=Client(Response(status=403))) == []
print("OK   cross-user stays silent when B is refused (403)")

# a READ where B gets back exactly A's data -> violation (data exposure)
a_read = Request("GET", "/users/v1/{username}", path_params={"username": "name1"},
                 headers={"Authorization": "Bearer A"}, op_key=protected_get.key)
a_body = Response(status=200, json={"username": "name1", "email": "secret@x.com"})
v = run(CrossUserAccessOracle(), one(a_read, a_body),
        alt_client=Client(Response(status=200, json={"username": "name1", "email": "secret@x.com"})))
assert v and v[0].severity == "violation" and "read back the exact data" in v[0].summary, v
print("OK   cross-user flags B reading back A's exact private data")

# a READ where B gets a 2XX but *different* data -> warning, not violation
v = run(CrossUserAccessOracle(), one(a_read, a_body),
        alt_client=Client(Response(status=200, json={"username": "name2", "email": "own@x.com"})))
assert v and v[0].severity == "warning", v
print("OK   cross-user downgrades to a warning when B sees different data")

# public op is never a cross-user finding
assert asyncio.run(CrossUserAccessOracle().check(
    one(pub_req, ok), OracleContext(ops=PUB, alt_client=Client(Response(status=200))))) == []
print("OK   cross-user never flags a public endpoint")

print("ALL CHECKS PASSED")
