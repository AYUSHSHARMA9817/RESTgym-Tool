"""Unit tests for the engine modules. Run: pytest tests/test_engine.py"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from restleague.models import Mode, Operation, Parameter, Request, RequestBody, Response
from restleague.engine.pool import ResourcePool, resource_of, resource_for_param, norm, singular
from restleague.engine.faults import FaultTracker, Bandit, normalise, signature
from restleague.engine import mutations as M
from restleague.engine import fallback


# ------------------------------------------------------------------ pool
def _resp(status=200, js=None, headers=None, text=""):
    return Response(status=status, json=js, headers=headers or {}, text=text)


def test_resource_helpers():
    assert resource_of("/owners/{ownerId}") == "owner"
    assert resource_of("/api/v1/owners/{id}/pets") == "pet"
    assert resource_for_param("/owners/{ownerId}", "ownerId") == "owner"
    assert singular("categories") == "category"
    assert norm("owner_Id-x") == "owneridx"


def test_pool_harvest_and_lookup():
    pool = ResourcePool(seed=1)
    op = Operation("POST", "/owners")
    pool.harvest(op, Request("POST", "/owners", body={"firstName": "Jo"}), _resp(201, {"id": 7, "name": "Jo"}))
    get_op = Operation("GET", "/owners/{ownerId}")
    assert pool.lookup("ownerId", {"type": "integer"}, get_op) == 7
    assert pool.lookup("id", {"type": "integer"}, get_op) == 7


def test_pool_nested_and_location_header():
    pool = ResourcePool(seed=2)
    op = Operation("POST", "/pets")
    pool.harvest(op, Request("POST", "/pets"), _resp(201, {"pet": {"id": 5}}, {"location": "/pets/5"}))
    assert pool.lookup("petId", {"type": "integer"}, Operation("GET", "/pets/{petId}")) == 5


def test_pool_forget_on_delete():
    pool = ResourcePool(seed=3)
    pool.add("ownerId", 9)
    pool.harvest(Operation("DELETE", "/owners/{ownerId}"), Request("DELETE", "/owners/{ownerId}", path_params={"ownerId": 9}), _resp(204))
    assert pool.lookup("ownerId", {"type": "integer"}, Operation("GET", "/owners/{ownerId}")) is None


def test_pool_type_filtering():
    pool = ResourcePool(seed=4)
    pool.add("name", "hello")
    assert pool.lookup("name", {"type": "integer"}) is None
    assert pool.lookup("name", {"type": "string"}) == "hello"


# ----------------------------------------------------------------- faults
def test_signature_normalisation_dedups():
    r1 = _resp(500, {"message": "id 123 not found at 2021-01-01T00:00:00Z"})
    r2 = _resp(500, {"message": "id 999 not found at 2024-07-07T12:00:00Z"})
    assert signature("GET /x", r1) == signature("GET /x", r2)


def test_signature_distinct_messages():
    r1 = _resp(500, {"message": "division by zero"})
    r2 = _resp(500, {"message": "index out of range"})
    assert signature("POST /x", r1) != signature("POST /x", r2)


def test_fault_tracker_counts_unique():
    ft = FaultTracker()
    req = Request("POST", "/x")
    assert ft.record("POST /x", req, _resp(500, {"error": "boom 1"})) is True
    # "boom 1"/"boom 2" differ only by a number -> same normalised signature
    assert ft.record("POST /x", req, _resp(500, {"error": "boom 2"})) is False
    assert ft.record("POST /x", req, _resp(500, {"error": "totally different crash"})) is True
    assert ft.record("POST /x", req, _resp(200)) is False
    assert ft.unique == 2


def test_bandit_prefers_rewarded_arm():
    b = Bandit(["a", "b", "c"], seed=0)
    for _ in range(200):
        arm = b.select()
        b.update(arm, 1.0 if arm == "b" else 0.0)
    counts = {a: b.n[a] for a in ("a", "b", "c")}
    assert counts["b"] == max(counts.values())


# -------------------------------------------------------------- mutations
def _sample_request():
    return Request("PUT", "/owners/{ownerId}", path_params={"ownerId": 3},
                   query={"verbose": True}, body={"firstName": "Jo", "age": 30, "tags": ["x"]},
                   content_type="application/json", op_key="PUT /owners/{ownerId}")


def _sample_op():
    return Operation("PUT", "/owners/{ownerId}",
                     parameters=[Parameter("ownerId", "path", True, {"type": "integer"}),
                                 Parameter("verbose", "query", False, {"type": "boolean"})],
                     request_body=RequestBody(schema={"type": "object",
                                                      "properties": {"firstName": {"type": "string", "maxLength": 30},
                                                                     "age": {"type": "integer", "minimum": 0, "maximum": 150},
                                                                     "tags": {"type": "array", "items": {"type": "string"}}},
                                                      "required": ["firstName", "age"]}))


def test_every_mutation_runs_without_error():
    rng = random.Random(0)
    op, pool = _sample_op(), ResourcePool(seed=0)
    pool.add("ownerId", 11)
    produced = 0
    for name in M.OPERATORS:
        for _ in range(20):
            out = M.mutate(name, _sample_request(), op, pool, rng, stack=rng.choice([1, 2]))
            if out is not None:
                assert isinstance(out, Request)
                assert out.mutation
                produced += 1
    assert produced > 0


def test_mutation_does_not_touch_original():
    rng = random.Random(1)
    base = _sample_request()
    M.mutate("boundary_number", base, _sample_op(), ResourcePool(), rng)
    assert base.body["age"] == 30  # deep-copied


def test_injection_payloads_present_and_reachable():
    # the tool is a security tester: injection-detection probes must ship and be usable
    blob = " ".join(map(str, M.INJECTION_PAYLOADS))
    for probe in ("OR '1'='1", "<script", "etc/passwd", "{{7*7}}", "sleep 3"):
        assert probe in blob
    rng = random.Random(0)
    op = _sample_op()
    seen = {"injection": 0, "injection_raw_body": 0}
    for _ in range(80):
        for name in ("injection", "injection_raw_body"):
            out = M.mutate(name, _sample_request(), op, ResourcePool(), rng)
            if out is not None:
                seen[name] += 1
    assert seen["injection"] > 0 and seen["injection_raw_body"] > 0


# --------------------------------------------------------------- fallback
def test_fallback_parses_and_generates():
    spec = str(Path(__file__).with_name("buggy_openapi.json"))
    ops = fallback.order_operations(fallback.parse_spec(spec))
    assert ops and ops[0].method == "POST"  # creators first
    pool = ResourcePool(seed=0)
    for op in ops:
        for mode in Mode:
            req = fallback.generate_request(op, pool, mode)
            assert req.method == op.method


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_end_to_end_finds_fault_and_covers():
    """Serve the buggy API in-thread and run a short budget; must cover ops and find a fault."""
    import socket, threading, time
    import uvicorn
    from tests.buggy_api import app
    from restleague.engine.strategy import run

    port = 8155
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        ops = fallback.order_operations(fallback.parse_spec(str(Path(__file__).with_name("buggy_openapi.json"))))
        res = run(ops, budget_seconds=12, base_url=f"http://127.0.0.1:{port}", workers=6, seed=7, nominal_fraction=0.3)
    finally:
        srv.should_exit = True
    assert res.operations_covered >= 6
    assert res.unique_5xx >= 1
    assert res.total_requests > 500


# ------------------------------------------------------ dedup modes & back-off
def test_dedup_mode_op_message_vs_message():
    # same message on two different ops: 2 under op_message, 1 under message-only
    r = lambda: _resp(500, {"message": "boom at line 10"})
    t_op = FaultTracker(dedup_mode="op_message")
    t_op.record("GET /a", Request("GET", "/a"), r())
    t_op.record("GET /b", Request("GET", "/b"), r())
    assert t_op.unique == 2

    t_msg = FaultTracker(dedup_mode="message")
    t_msg.record("GET /a", Request("GET", "/a"), r())
    t_msg.record("GET /b", Request("GET", "/b"), r())
    assert t_msg.unique == 1

    t_os = FaultTracker(dedup_mode="op_status")
    t_os.record("GET /a", Request("GET", "/a"), _resp(500, {"message": "x"}))
    t_os.record("GET /a", Request("GET", "/a"), _resp(500, {"message": "totally other"}))
    assert t_os.unique == 1  # one fault per crashing endpoint
    assert t_os.summary()["dedup_mode"] == "op_status"


def test_dedup_mode_from_env(monkeypatch=None):
    import os
    os.environ["FAULT_DEDUP"] = "message"
    try:
        assert FaultTracker().dedup_mode == "message"
    finally:
        del os.environ["FAULT_DEDUP"]


def _timeout_resp(elapsed=8.0):
    return Response(status=0, error="timeout: ReadTimeout", elapsed=elapsed)


def test_slow_backoff_lowers_op_weight():
    from restleague.engine.strategy import Strategy
    op = Operation("PUT", "/users/{u}/email",
                   parameters=[Parameter("u", "path", True, {"type": "string"})])
    other = Operation("GET", "/users")
    s = Strategy([op, other], "http://x", budget_seconds=10, seed=0)
    s.stats[op.key].reached = True
    s.stats[other.key].reached = True
    base = s._op_weight(op.key)
    # simulate repeated timeouts on the email op
    for _ in range(5):
        s.stats[op.key].timeouts += 1
    assert s._op_weight(op.key) < base
    # a mutation that keeps timing out is marked for avoidance
    s.stats[op.key].mut_timeouts["string_attack"] = 3
    assert "string_attack" in s._slow_mutations(op.key)


def test_execute_counts_timeout(monkeypatch=None):
    import asyncio
    from restleague.engine.strategy import Strategy

    class FakeClient:
        total_requests = 0
        total_transport_errors = 0
        api_down = False
        async def send(self, req):
            return _timeout_resp(9.0)

    op = Operation("GET", "/slow")
    s = Strategy([op], "http://x", budget_seconds=5, seed=0, client=FakeClient())
    req = Request("GET", "/slow", op_key=op.key, mutation="string_attack")
    asyncio.run(s.execute(req, op))
    assert s.stats[op.key].timeouts == 1
    assert s.stats[op.key].mut_timeouts.get("string_attack") == 1
