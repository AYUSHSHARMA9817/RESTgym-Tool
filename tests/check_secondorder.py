"""Check the second-order injection oracle and its taint recording.

Run: python3 -m tests.check_secondorder
"""
import asyncio

from restleague.models import Operation, Request, Response, Sequence, Step
from restleague.oracles import OracleContext, SecondOrderInjectionOracle
from restleague.oracles.secondorder import looks_injected, record_taint

# ------------------------------------------------- taint recognition
assert looks_injected("' OR 1=1--") and looks_injected("<script>")
assert not looks_injected("Alice") and not looks_injected("alice@example.com")
assert not looks_injected(42) and not looks_injected(None)
print("OK   taint recognises hostile strings, ignores ordinary values")

# only a SUCCESSFUL write records taint
t = {}
post = Operation("POST", "/users")
record_taint(t, post, Request("POST", "/users", body={"name": "' OR 1=1--"}), Response(status=201))
assert t.get("user") == {"' OR 1=1--"}, t
record_taint(t, post, Request("POST", "/users", body={"name": "<x>"}), Response(status=400))
assert t.get("user") == {"' OR 1=1--"}, "a rejected write stores nothing"
print("OK   taint is recorded only for accepted writes")


# ------------------------------------------------- the oracle
list_users = Operation("GET", "/users")
OPS = {o.key: o for o in (post, list_users)}


def one(req, resp):
    s = Sequence(target_op_key=req.op_key); s.append(Step(req, resp)); return s


def run(seq, taint):
    return asyncio.run(SecondOrderInjectionOracle().check(
        seq, OracleContext(ops=OPS, taint=taint)))


taint = {"user": {"' OR 1=1--"}}
clean_get = Request("GET", "/users", op_key="GET /users")

# clean read crashes AND the error echoes the stored payload -> violation
v = run(one(clean_get, Response(status=500, text="SQL error near ' OR 1=1--")), taint)
assert v and v[0].severity == "violation" and "echoes a payload" in v[0].summary, v
print("OK   second-order: crash echoing a stored payload -> violation")

# clean read crashes, payload not in body, but taint exists for the resource -> warning
v = run(one(clean_get, Response(status=500, text="Internal Server Error")), taint)
assert v and v[0].severity == "warning", v
print("OK   second-order: crash after a stored payload, no echo -> warning")

# a read that carried its OWN payload is first-order, not ours
dirty = Request("GET", "/users", op_key="GET /users", mutation="sweep:sqli")
assert run(one(dirty, Response(status=500, text="boom")), taint) == []
print("OK   second-order ignores a request carrying its own payload (first-order)")

# no taint at all -> nothing
assert run(one(clean_get, Response(status=500, text="boom")), {}) == []
# clean read that did NOT crash -> nothing
assert run(one(clean_get, Response(status=200, json=[])), taint) == []
print("OK   second-order quiet with no taint or no crash")

print("ALL CHECKS PASSED")
