"""Check that findings persist, replay from a clean API, and verify honestly.

Run: python3 -m tests.check_findings
"""
import asyncio
import json
import os
import tempfile

from restleague.findings import Finding, FindingStore, RequestRecord, replay, verify
from restleague.models import Operation, OracleViolation, Parameter, RequestBody, Response, Sequence, Step
from restleague.oracles import ALL_ORACLES
from restleague.track_a import TrackARunner
from tests.check_track_a import OPS, BuggyApi

ORACLES = {cls.name: cls() for cls in ALL_ORACLES}
OPS_BY_KEY = {o.key: o for o in OPS}

# ------------------------------------------------------------- persistence
tmp = os.path.join(tempfile.mkdtemp(), "findings.jsonl")
store = FindingStore(tmp, api="demo")
seq = Sequence()
seq.append(Step(Request := __import__("restleague.models", fromlist=["Request"]).Request(
    "POST", "/owners", body={"firstName": "Leo"}, op_key="POST /owners"),
    Response(status=201, json={"id": 1})))
f = store.record(OracleViolation(oracle="crud_roundtrip", op_key="POST /owners",
                                 summary="lastName not persisted"), seq)
assert os.path.exists(tmp), "findings must be written as they arrive"
reloaded = FindingStore(tmp).load()
assert len(reloaded) == 1 and reloaded[0].summary == "lastName not persisted"
assert reloaded[0].requests[0].method == "POST"
print("OK   findings are written on arrival and reload intact")

cmds = f.as_curl("http://localhost:8080")
assert cmds and cmds[0].startswith("curl -i -X POST 'http://localhost:8080/owners'")
assert "firstName" in cmds[0]
print("OK   a finding renders as a runnable reproduction:")
print("      ", cmds[0][:88])


# ------------------------------------------------- ids are rebound on replay
async def rebinding():
    """A replay must not reuse recorded ids; a clean server issues new ones."""
    api = BuggyApi()
    # recorded against a server where the owner happened to be id 77
    f = Finding(oracle="crud_roundtrip", op_key="POST /owners/{ownerId}/pets",
                summary="x", severity="violation", evidence={},
                requests=[RequestRecord("POST", "/owners", {}, {}, {},
                                        {"firstName": "Leo"}, None, "POST /owners"),
                          RequestRecord("POST", "/owners/{ownerId}/pets", {"ownerId": 77},
                                        {}, {}, {"name": "Rex"}, None,
                                        "POST /owners/{ownerId}/pets")])
    seq = await replay(f, OPS_BY_KEY, api)
    assert len(seq.steps) == 2, seq.steps
    created = seq.steps[0].response.json["id"]
    used = seq.steps[1].request.path_params["ownerId"]
    assert used == created, f"replay must use the newly created owner {created}, not 77 (got {used})"
    assert seq.steps[1].response.ok, "the chained request should succeed on the fresh API"
    print(f"OK   replay rebound ownerId 77 -> {used} (the id this run created)")


asyncio.run(rebinding())


# --------------------------------------------------- verification is honest
async def verification():
    # a real bug: BuggyApi leaves owners readable after DELETE
    api = BuggyApi()
    runner = TrackARunner(OPS, "http://fake", budget_seconds=1.5, seed=3,
                          client=api, workers=1, api="buggy")
    res = await runner.run_async()
    found = runner.findings.findings
    assert found, "the planted bugs should have produced findings"
    assert all(f.requests for f in found), "every finding must carry its requests"
    print(f"OK   {len(found)} findings captured, each with its request sequence")

    confirmed = 0
    for f in found:
        if await verify(f, OPS_BY_KEY, BuggyApi(), ORACLES):   # fresh API each time
            confirmed += 1
    print(f"OK   verification against a clean API: {confirmed}/{len(found)} reproduced")
    assert confirmed > 0, "planted bugs must reproduce from a clean start"
    assert all(f.verified is not None for f in found), "each finding records a verdict"

    # and a finding that was never real must fail verification
    bogus = Finding(oracle="zombie_resource", op_key="GET /owners/{ownerId}",
                    summary="invented", severity="violation", evidence={},
                    requests=[RequestRecord("GET", "/owners/{ownerId}", {"ownerId": 1},
                                            {}, {}, None, None, "GET /owners/{ownerId}")])
    assert await verify(bogus, OPS_BY_KEY, BuggyApi(), ORACLES) is False
    assert bogus.verified is False and "not reproduce" in bogus.verify_note
    print("OK   an invented finding does not survive verification")


asyncio.run(verification())
print("ALL CHECKS PASSED")
