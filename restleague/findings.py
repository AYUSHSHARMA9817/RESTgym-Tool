"""Findings that persist, replay and verify themselves.

A violation on its own is a claim: "this field was not persisted", "this entity
survived its own DELETE". Nothing about it is checkable by anyone who did not
watch the run, and it is lost when the process exits. This module records the
exact requests that produced each finding, writes them out as they are found,
and replays them against a fresh API to see whether the finding still stands.

Replaying needs more than saved requests, because a fresh server issues
different ids: the owner that was 7 during the run is 1 on a clean container.
So a replay re-derives ids as it goes, through the same
RelationalKnowledgeGraph the tool uses normally -- path parameters are rebound
from entities created earlier *in the replay*, not from the recording.

A finding that reproduces this way is independently demonstrable: a short
sequence of requests, from a clean state, that anyone can run. One that does
not reproduce depended on accumulated state or noise and should not be
reported as a bug.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .models import Operation, OracleViolation, Request, Sequence, Step
from .sequence.executor import harvest_to_graph
from .state.graph import RelationalKnowledgeGraph


@dataclass
class RequestRecord:
    """A request in a form that survives JSON and can be rebuilt."""
    method: str
    path: str
    path_params: dict = field(default_factory=dict)
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: Any = None
    content_type: Optional[str] = None
    op_key: str = ""
    mutation: Optional[str] = None

    @classmethod
    def of(cls, req: Request) -> "RequestRecord":
        return cls(method=req.method, path=req.path, path_params=dict(req.path_params),
                   query=dict(req.query), headers=dict(req.headers), body=req.body,
                   content_type=req.content_type, op_key=req.op_key, mutation=req.mutation)

    def build(self) -> Request:
        return Request(method=self.method, path=self.path, path_params=dict(self.path_params),
                       query=dict(self.query), headers=dict(self.headers), body=self.body,
                       content_type=self.content_type, op_key=self.op_key,
                       mutation=self.mutation)


@dataclass
class Finding:
    """One violation plus everything needed to demonstrate it again."""
    oracle: str
    op_key: str
    summary: str
    severity: str
    evidence: dict
    requests: list                      # RequestRecord, in order
    api: str = ""
    found_at: float = 0.0
    verified: Optional[bool] = None     # None = not yet replayed
    verify_note: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["requests"] = [asdict(r) if not isinstance(r, dict) else r for r in self.requests]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Finding":
        """Rebuild from stored JSON, ignoring keys we did not write ourselves.

        Reports add their own fields (a rendered reproduction, reviewer notes),
        and a stored finding has to survive being read back after that.
        """
        known = {f for f in cls.__dataclass_fields__}
        d = {k: v for k, v in d.items() if k in known}
        d["requests"] = [r if isinstance(r, RequestRecord) else RequestRecord(**r)
                         for r in d.get("requests", [])]
        return cls(**d)

    #: oracles whose reproduction needs credentials the recording cannot hold
    _AUTH_ORACLES = {"missing_auth", "cross_user_access", "cross_entity_access"}

    def as_curl(self, base_url: str = "http://HOST:PORT") -> list[str]:
        """The reproduction as shell commands, for pasting into a report.

        Auth is applied by the client at send time, so tokens are not in the
        recorded requests. For an authorization finding the token *is* the
        point, so the setup requests carry a ``$TOKEN`` placeholder and the
        final (attacking) request a ``$TOKEN_B`` one, with a note saying what
        each is -- rather than silently emitting a request that would 401.
        """
        is_auth = self.oracle in self._AUTH_ORACLES
        out: list[str] = []
        if is_auth:
            if self.oracle == "cross_user_access":
                out.append("# $TOKEN = user A's token, $TOKEN_B = a different user's token")
            elif self.oracle == "missing_auth":
                out.append("# $TOKEN = any valid token; the final request omits it on purpose")
            else:
                out.append("# $TOKEN = a valid token")
        last = len(self.requests) - 1
        for i, r in enumerate(self.requests):
            rec = r if isinstance(r, RequestRecord) else RequestRecord(**r)
            path = rec.path
            for k, v in rec.path_params.items():
                path = path.replace("{" + k + "}", str(v))
            cmd = f"curl -i -X {rec.method} '{base_url}{path}'"
            if is_auth:
                if self.oracle == "cross_user_access" and i == last:
                    cmd += " -H 'Authorization: Bearer $TOKEN_B'"   # the attacking request
                elif self.oracle == "missing_auth" and i == last:
                    pass                                            # no token: that is the test
                else:
                    cmd += " -H 'Authorization: Bearer $TOKEN'"
            if rec.body is not None:
                cmd += (" -H 'Content-Type: application/json'"
                        f" -d '{json.dumps(rec.body)}'")
            out.append(cmd)
        return out


class FindingStore:
    """Append-only JSONL store. Written as findings arrive, not at the end."""

    def __init__(self, path: Optional[str] = None, api: str = ""):
        self.path = path or os.environ.get("FINDINGS_FILE")
        self.api = api
        self.findings: list[Finding] = []
        self._lock = threading.Lock()

    def record(self, violation: OracleViolation, seq: Sequence) -> Finding:
        f = Finding(oracle=violation.oracle, op_key=violation.op_key,
                    summary=violation.summary, severity=violation.severity,
                    evidence=dict(violation.evidence),
                    requests=[RequestRecord.of(s.request) for s in seq.steps],
                    api=self.api, found_at=time.time())
        with self._lock:
            self.findings.append(f)
            if self.path:
                try:
                    with open(self.path, "a") as fh:
                        fh.write(json.dumps(f.to_json(), default=str) + "\n")
                except OSError:
                    pass
        return f

    def load(self) -> list[Finding]:
        if not self.path or not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(Finding.from_json(json.loads(line)))
                    except Exception:
                        continue
        return out


async def replay(finding: Finding, ops_by_key: dict, client) -> Sequence:
    """Re-issue a finding's requests against a clean API, rebinding ids.

    Saved ids are meaningless on a fresh server, so each request's path
    parameters are resolved from entities created earlier *in this replay*.
    Anything the graph cannot supply keeps its recorded value, which is right
    for parameters that were never resource ids in the first place.
    """
    rkg = RelationalKnowledgeGraph(seed=0)
    seq = Sequence(target_op_key=finding.op_key)
    # a race finding only means anything if its final request is fired twice at
    # once, as the original run did -- replaying it once could never reproduce
    # "both concurrent copies succeeded"
    race = finding.oracle == "race_condition"
    records = list(finding.requests)
    for i, rec in enumerate(records):
        record = rec if isinstance(rec, RequestRecord) else RequestRecord(**rec)
        req = record.build()
        try:
            bindings = rkg.query_path_bindings(req.path)
        except Exception:
            bindings = None
        if bindings:
            req.path_params.update(bindings)

        if race and i == len(records) - 1:
            import asyncio
            req.mutation = "double_action"
            twin = _copy_request(req)
            first, second = await asyncio.gather(client.send(req), client.send(twin))
            seq.append(Step(request=req, response=first, bindings=dict(req.path_params)))
            tw = Step(request=twin, response=second, bindings=dict(twin.path_params))
            tw.bindings["_twin_status"] = first.status
            seq.append(tw)
            break

        resp = await client.send(req)
        seq.append(Step(request=req, response=resp, bindings=dict(req.path_params)))
        op = ops_by_key.get(req.op_key)
        if op is not None:
            try:
                harvest_to_graph(rkg, op, req, resp)
            except Exception:
                pass
    return seq


def _copy_request(req: Request) -> Request:
    import copy
    return copy.deepcopy(req)


async def verify(finding: Finding, ops_by_key: dict, client, oracles: dict,
                 unauth_client=None, alt_client=None) -> bool:
    """Replay a finding and re-run its oracle. True when it still stands.

    The oracle judges the replayed final step, exactly as it did during the
    run, and the finding is confirmed only when a violation with the same
    oracle and operation comes back. Anything that needed state built up
    earlier in a long run, or that was simply noise, fails here -- which is the
    point: what survives is demonstrable from a clean start.

    ``unauth_client``/``alt_client`` must be supplied to verify a finding from
    an authorization oracle -- those replay through a second client, and without
    it the oracle silently skips and the finding looks unreproducible.
    """
    from .oracles.base import OracleContext

    oracle = oracles.get(finding.oracle)
    if oracle is None:
        finding.verified, finding.verify_note = None, "no such oracle"
        return False
    seq = await replay(finding, ops_by_key, client)
    if not seq.steps:
        finding.verified, finding.verify_note = False, "nothing replayed"
        return False
    last = Sequence(target_op_key=finding.op_key)
    last.append(seq.steps[-1])
    ctx = OracleContext(ops=ops_by_key, client=client, verify=False,
                        unauth_client=unauth_client, alt_client=alt_client)
    try:
        again = await oracle.check(last, ctx)
    except Exception as e:
        finding.verified, finding.verify_note = False, f"oracle raised: {e!r}"
        return False
    hit = any(v.oracle == finding.oracle and v.op_key == finding.op_key for v in again)
    finding.verified = hit
    finding.verify_note = "reproduced from a clean API" if hit else "did not reproduce"
    return hit
