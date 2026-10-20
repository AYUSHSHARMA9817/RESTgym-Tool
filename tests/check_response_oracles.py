"""Check the response-inspection oracles: reflected payload, timing, CORS.

Run: python3 -m tests.check_response_oracles
"""
import asyncio

from restleague.models import Request, Response, Sequence, Step
from restleague.oracles import CorsOracle, OracleContext, ReflectedPayloadOracle, TimingInjectionOracle


def one(req, resp):
    s = Sequence(target_op_key=req.op_key); s.append(Step(req, resp)); return s


def run(oracle, req, resp):
    return asyncio.run(oracle.check(one(req, resp), OracleContext()))


# ------------------------------------------------- reflected XSS (B5)
xss = "<script>alert(1)</script>"
req = Request("POST", "/comments", body={"text": xss}, op_key="POST /comments")

# verbatim reflection in an HTML response -> violation
v = run(ReflectedPayloadOracle(), req,
        Response(status=200, text=f"<div>{xss}</div>", headers={"content-type": "text/html"}))
assert v and v[0].severity == "violation" and "HTML response reflects" in v[0].summary, v
print("OK   reflected-payload flags verbatim XSS in an HTML response")

# same reflection but JSON content-type -> warning, not violation
v = run(ReflectedPayloadOracle(), req,
        Response(status=200, text=f'{{"text": "{xss}"}}', headers={"content-type": "application/json"}))
assert v and v[0].severity == "warning", v
print("OK   reflected-payload downgrades a non-HTML echo to a warning")

# server escaped the payload -> no verbatim match -> silent
v = run(ReflectedPayloadOracle(), req,
        Response(status=200, text="<div>&lt;script&gt;alert(1)&lt;/script&gt;</div>",
                 headers={"content-type": "text/html"}))
assert v == [], v
print("OK   reflected-payload stays silent when the payload is escaped")

# an ordinary value that happens to appear is not flagged (no XSS markers)
plain = Request("POST", "/x", body={"name": "Alice"}, op_key="POST /x")
assert run(ReflectedPayloadOracle(), plain,
           Response(status=200, text="Alice", headers={"content-type": "text/html"})) == []
print("OK   reflected-payload ignores ordinary reflected values")


# -------------------------------------------------- timing injection (B4)
sleepy = Request("GET", "/users", query={"q": "1' AND SLEEP(3)--"}, op_key="GET /users")
v = run(TimingInjectionOracle(), sleepy, Response(status=200, elapsed=3.1))
assert v and v[0].severity == "violation" and "time-based blind" in v[0].summary, v
print("OK   timing flags a sleep payload whose response was slow")

# slow response but no sleep payload -> not ours (just a slow endpoint)
assert run(TimingInjectionOracle(), Request("GET", "/users", query={"q": "ok"}, op_key="GET /users"),
           Response(status=200, elapsed=3.1)) == []
# sleep payload but fast response -> not vulnerable
assert run(TimingInjectionOracle(), sleepy, Response(status=200, elapsed=0.05)) == []
print("OK   timing needs both a sleep payload AND a slow response")


# --------------------------------------------------------- CORS (E6)
get = Request("GET", "/data", op_key="GET /data")
v = run(CorsOracle(), get, Response(status=200, headers={
    "access-control-allow-origin": "*", "access-control-allow-credentials": "true"}))
assert v and v[0].severity == "violation" and "credentialed cross-origin" in v[0].summary, v
print("OK   cors flags wildcard origin with credentials")

# wildcard without credentials -> allowed, silent
assert run(CorsOracle(), get, Response(status=200, headers={"access-control-allow-origin": "*"})) == []
# a specific origin with credentials -> allowed, silent
assert run(CorsOracle(), get, Response(status=200, headers={
    "access-control-allow-origin": "https://app.example.com",
    "access-control-allow-credentials": "true"})) == []
print("OK   cors stays silent on the allowed configurations")

print("ALL CHECKS PASSED")
