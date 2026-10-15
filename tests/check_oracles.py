"""Check the metamorphic oracles fire on real bugs and stay quiet on the
tolerated cases (docs/architecture/track_a_stateful_fuzzing.md, Milestone 3).

Run: python3 -m tests.check_oracles
"""
import asyncio

from restleague.models import Operation, Parameter, Request, RequestBody, Response, Sequence, Step
from restleague.oracles import (CrudRoundtripOracle, IdempotencyOracle, OracleContext,
                                RepeatedDeleteOracle, SafetyOracle, SchemaConformanceOracle,
                                ZombieResourceOracle, stable_view, state_diff)
from restleague.oracles.conformance import looks_like_stack_trace

# ------------------------------------------------------- tolerance helpers
assert stable_view({"id": 1, "name": "x", "updatedAt": "2026-01-01"}) == {"id": 1, "name": "x"}
assert state_diff({"name": "x", "version": 1}, {"name": "x", "version": 2}) == []
assert state_diff({"name": "x"}, {"name": "y"}) != []
# readOnly fields in the schema are server-owned and must be ignored too
schema = {"properties": {"id": {"readOnly": True}, "name": {}}}
assert state_diff({"id": 1, "name": "x"}, {"id": 2, "name": "x"}, schema) == []
print("OK   stable_view/state_diff ignore volatile and readOnly fields")

assert looks_like_stack_trace("java.lang.NullPointerException\n\tat com.foo.Bar(Bar.java:42)")
assert looks_like_stack_trace("Traceback (most recent call last):\n  File ...")
assert not looks_like_stack_trace('{"message": "owner not found"}')
print("OK   stack-trace detection does not fire on ordinary error bodies")

# ------------------------------------------------------------------ setup
owners = Operation("POST", "/owners", request_body=RequestBody(schema={
    "properties": {"firstName": {}, "lastName": {}, "id": {"readOnly": True}}}),
    responses={"201": {"properties": {"id": {"type": "integer"}, "firstName": {"type": "string"}}}})
owner_item = Operation("GET", "/owners/{ownerId}",
                       parameters=[Parameter("ownerId", "path", True, {"type": "integer"})],
                       responses={"200": {"properties": {"id": {"type": "integer"},
                                                         "firstName": {"type": "string"}}}})
owner_put = Operation("PUT", "/owners/{ownerId}",
                      parameters=[Parameter("ownerId", "path", True, {"type": "integer"})],
                      responses={"200": {"properties": {"id": {"type": "integer"}}}})
owner_del = Operation("DELETE", "/owners/{ownerId}",
                      parameters=[Parameter("ownerId", "path", True, {"type": "integer"})])
CTX_OPS = {o.key: o for o in (owners, owner_item, owner_put, owner_del)}


class ScriptedClient:
    """Returns queued responses; records what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    async def send(self, req):
        self.sent.append(req)
        return self.responses.pop(0) if self.responses else Response(status=404)


def seq_of(*steps):
    s = Sequence()
    for st in steps:
        s.append(st)
    return s


def run(oracle, seq, client=None, **kw):
    ctx = OracleContext(ops=CTX_OPS, client=client, zombie_backoff=0.0, **kw)
    return asyncio.run(oracle.check(seq, ctx))


# -------------------------------------------------------- conformance
post_req = Request("POST", "/owners", body={"firstName": "George", "lastName": "F"},
                   op_key=owners.key)
bad_schema = seq_of(Step(post_req, Response(status=201, json={"id": "not-an-int",
                                                              "firstName": "George"})))
v = run(SchemaConformanceOracle(), bad_schema)
assert v and "schema" in v[0].summary, v
print("OK   conformance oracle flags a 2XX body that breaks its declared schema")

good = seq_of(Step(post_req, Response(status=201, json={"id": 1, "firstName": "George"})))
assert run(SchemaConformanceOracle(), good) == []
print("OK   conformance oracle stays quiet on a conforming response")

leak = seq_of(Step(post_req, Response(status=400, text="java.lang.NullPointerException\n\tat a.B(B.java:1)")))
v = run(SchemaConformanceOracle(), leak)
assert v and "stack trace" in v[0].summary, v
print("OK   conformance oracle catches a crash disguised under a 400")

# -------------------------------------------------------------- zombie
del_req = Request("DELETE", "/owners/{ownerId}", path_params={"ownerId": 1}, op_key=owner_del.key)
dead = seq_of(Step(del_req, Response(status=204)))

# still 200 on every re-read -> real zombie
v = run(ZombieResourceOracle(), dead, ScriptedClient([Response(status=200, json={"id": 1})] * 3))
assert v and "still readable" in v[0].summary, v
print("OK   zombie oracle flags an entity that survives its own DELETE")

# gone on the first re-read -> clean
assert run(ZombieResourceOracle(), dead, ScriptedClient([Response(status=404)])) == []
# visible once, then gone (eventual consistency) -> tolerated, not flagged
client = ScriptedClient([Response(status=200, json={"id": 1}), Response(status=404)])
assert run(ZombieResourceOracle(), dead, client) == []
assert len(client.sent) == 2, "should have retried before giving a verdict"
print("OK   zombie oracle tolerates a lagging read replica (retries before flagging)")

# ------------------------------------------------------- repeated delete
v = run(RepeatedDeleteOracle(), dead, ScriptedClient([Response(status=500, text="boom")]))
assert v and "already-deleted" in v[0].summary, v
assert run(RepeatedDeleteOracle(), dead, ScriptedClient([Response(status=404)])) == []
print("OK   repeated-DELETE oracle flags only a 5XX on the second delete")

# ---------------------------------------------------------- idempotency
put_req = Request("PUT", "/owners/{ownerId}", path_params={"ownerId": 1},
                  body={"firstName": "George"}, op_key=owner_put.key)
put_seq = seq_of(Step(put_req, Response(status=200, json={"id": 1, "visits": 1,
                                                          "updatedAt": "t1"})))
# second PUT appends instead of replacing -> not idempotent
v = run(IdempotencyOracle(), put_seq,
        ScriptedClient([Response(status=200, json={"id": 1, "visits": 2, "updatedAt": "t2"})]))
assert v and "not idempotent" in v[0].summary, v
print("OK   idempotency oracle flags a PUT whose repeat changes state")

# same state except a bumped timestamp -> tolerated
v = run(IdempotencyOracle(), put_seq,
        ScriptedClient([Response(status=200, json={"id": 1, "visits": 1, "updatedAt": "t2"})]))
assert v == [], v
print("OK   idempotency oracle tolerates a bumped updatedAt timestamp")

v = run(IdempotencyOracle(), put_seq, ScriptedClient([Response(status=500, text="boom")]))
assert v and "crashed" in v[0].summary, v
print("OK   idempotency oracle flags a repeated PUT that 5XXs")

# --------------------------------------------------------------- safety
get_req = Request("GET", "/owners/{ownerId}", path_params={"ownerId": 1}, op_key=owner_item.key)
get_seq = seq_of(Step(get_req, Response(status=200, json={"id": 1, "views": 1})))
v = run(SafetyOracle(), get_seq, ScriptedClient([Response(status=200, json={"id": 1, "views": 2})]))
assert v and "different state" in v[0].summary, v
assert run(SafetyOracle(), get_seq,
           ScriptedClient([Response(status=200, json={"id": 1, "views": 1})])) == []
print("OK   safety oracle flags a GET that mutates state")

# ------------------------------------------------------------ roundtrip
create = seq_of(Step(post_req, Response(status=201, json={"id": 1})))
# server silently drops lastName
v = run(CrudRoundtripOracle(), create,
        ScriptedClient([Response(status=200, json={"id": 1, "firstName": "George"})]))
assert v and "lastName" in str(v[0].evidence), v
print("OK   crud oracle flags a written field that never came back")

# server stores a different value than the one sent
v = run(CrudRoundtripOracle(), create,
        ScriptedClient([Response(status=200, json={"id": 1, "firstName": "GEORGE",
                                                   "lastName": "F"})]))
assert v and "not persisted" in v[0].summary, v
print("OK   crud oracle flags a written field stored with a different value")

# everything persisted, plus server-generated extras -> clean
v = run(CrudRoundtripOracle(), create,
        ScriptedClient([Response(status=200, json={"id": 1, "firstName": "George",
                                                   "lastName": "F", "createdAt": "t"})]))
assert v == [], v
print("OK   crud oracle ignores server-generated extras on read back")

# Found against a live pet-clinic: on an item route the PATH decides which
# entity is written, so a server that ignores a conflicting "id" in the body is
# correct. Comparing it produced "id: sent 283, read back 659" on a healthy API.
put_item = Operation("PUT", "/pettypes/{petTypeId}",
                     parameters=[Parameter("petTypeId", "path", True, {"type": "integer"})],
                     request_body=RequestBody(schema={"properties": {"id": {}, "name": {}}}))
get_item = Operation("GET", "/pettypes/{petTypeId}",
                     parameters=[Parameter("petTypeId", "path", True, {"type": "integer"})])
ID_OPS = {o.key: o for o in (put_item, get_item)}
put_req = Request("PUT", "/pettypes/{petTypeId}", path_params={"petTypeId": 659},
                  body={"id": 283, "name": "cat"}, op_key=put_item.key)
seq_id = seq_of(Step(put_req, Response(status=200, json={"id": 659, "name": "cat"})))
ctx_id = OracleContext(ops=ID_OPS, client=ScriptedClient(
    [Response(status=200, json={"id": 659, "name": "cat"})]))
assert asyncio.run(CrudRoundtripOracle().check(seq_id, ctx_id)) == [], \
    "the path id wins on an item route; a differing body id is not a bug"
print("OK   crud oracle ignores a body id overridden by the path (real-API false positive)")


# ------------------------------------------- attribution (regression test)
# An oracle must judge a step against the state as it was when that step ran.
# Checking a finished sequence instead makes a later step's legitimate change
# look like the earlier step misbehaving: here a pure GET /owners is followed
# by a POST /owners, and replaying the GET afterwards shows a longer list.
# That is the API working correctly, and must not be reported.
owners_list = Operation("GET", "/owners", responses={"200": {}})
owners_post = Operation("POST", "/owners")
ATTR_OPS = {o.key: o for o in (owners_list, owners_post)}


class AppendingApi:
    """Correct API: GET is pure, POST appends a row."""

    def __init__(self):
        self.rows = [{"id": 1}]

    async def send(self, req):
        if req.method == "GET":
            return Response(status=200, json=list(self.rows))
        self.rows.append({"id": len(self.rows) + 1})
        return Response(status=201, json=self.rows[-1])


async def attribution_check():
    api = AppendingApi()
    ctx = OracleContext(ops=ATTR_OPS, client=api)
    get_req = Request("GET", "/owners", op_key=owners_list.key)
    post_req = Request("POST", "/owners", body={"firstName": "x"}, op_key=owners_post.key)

    # The order the executor's per-step hook produces: run the GET, judge it
    # straight away, and only then run the next step.
    get_resp = await api.send(get_req)
    inline = Sequence()
    inline.append(Step(get_req, get_resp))
    assert await SafetyOracle().check(inline, ctx) == [], \
        "a pure GET judged at the right moment must not be flagged"
    post_resp = await api.send(post_req)

    # The order the old whole-sequence check produced: the GET's recorded body
    # is compared against a replay taken after the POST appended a row, so the
    # POST's legitimate change is blamed on the GET.
    whole = Sequence()
    whole.append(Step(get_req, get_resp))
    whole.append(Step(post_req, post_resp))
    stale = await SafetyOracle().check(whole, ctx)
    assert stale, "sanity: a stale baseline is what produced the false positive"


asyncio.run(attribution_check())
print("OK   oracles judged per step do not blame a GET for a later POST's change")

print("ALL CHECKS PASSED")
