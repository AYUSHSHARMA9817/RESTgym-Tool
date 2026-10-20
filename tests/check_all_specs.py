"""Drive the whole Track A pipeline over every spec in specs/, against a
generic in-process API.

Only two of the fifteen bundled specs have runnable containers, so everything
built so far has been exercised against two APIs. This puts the remaining
thirteen -- Kafka REST proxy, genome-nexus, LanguageTool, restcountries and the
rest, with their own path shapes, deep schemas and odd parameter styles --
through parsing, planning, mutation, execution and all eight oracles, and fails
on anything that crashes, hangs or produces a nonsensical plan.

Run: python3 -m tests.check_all_specs
"""
import asyncio
import glob
import re
import sys
import time
from typing import Optional

from restleague.models import Response
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec
from restleague.sequence.synthesizer import MAX_LEN, SequenceSynthesizer
from restleague.track_a import TrackARunner

BUDGET = 3.0          # seconds of fuzzing per spec
PLAN_BUDGET = 5.0     # seconds allowed to plan every target in a spec
_PARAM_RE = re.compile(r"\{([^}]+)\}")


class GenericApi:
    """A deliberately *correct* REST API, for any spec.

    Correct in the specific senses the oracles test: what you write is what you
    read back, a deleted entity stops being readable, GET changes nothing, a
    second DELETE is a 404 rather than a crash, and -- importantly -- fields
    the spec never declared are discarded rather than stored. So if
    ``crud_roundtrip``, ``zombie_resource``, ``safety``, ``repeated_delete`` or
    ``mass_assignment`` fires here, the finding is ours and it is wrong.

    Two oracles are expected to fire and are not evidence of a bug:
    ``schema_conformance``, because a generic mock cannot satisfy fifteen
    different declared response schemas, and ``cross_entity_access``, because
    this mock does not enforce parent/child ownership.

    Unknown ids are 404s, so chaining ids properly is what earns coverage --
    unless the spec never creates anything at all (a computational API such as
    ``GET /api/bessj/{n}/{x}``, where the parameters are numbers rather than
    resource identifiers), in which case any value is accepted.
    """

    def __init__(self, declared: Optional[dict] = None, creates: bool = True):
        self.entities: dict[str, dict] = {}
        self.declared = declared or {}     # op key -> field names the spec declares
        self.counter = 0
        # Whether this *spec* creates resources, decided up front rather than
        # from runtime state. Deciding it at runtime made a PUT arriving before
        # the first POST return 200 without persisting, where a real API would
        # 404 -- and the round-trip oracle rightly called that a lost write.
        self.creates = creates
        self.total_requests = 0
        self.total_transport_errors = 0
        self.api_down = False

    async def probe(self):
        return True

    async def wait_until_alive(self, deadline):
        return True

    async def close(self):
        pass

    def _collection(self, path: str) -> str:
        return path.rstrip("/").rsplit("/", 1)[-1]

    def _accepted(self, req) -> dict:
        """Only fields the spec declares for this operation. Everything else is
        dropped, which is what a framework that is *not* mass-assignable does."""
        body = req.body if isinstance(req.body, dict) else {}
        allowed = self.declared.get(req.op_key)
        return {k: v for k, v in body.items()
                if not isinstance(v, (dict, list)) and (allowed is None or k in allowed)}

    async def send(self, req):
        self.total_requests += 1
        method, path = req.method.upper(), req.path
        params = _PARAM_RE.findall(path)
        # Unknown ids are 404s -- but only once this API mints ids at all. A
        # computational spec never creates anything, and its path parameters
        # are values rather than identifiers.
        # A POST (or PUT) whose path *ends* in a parameter creates a resource
        # under a client-chosen identifier -- POST /products/{productName}.
        # That id is new by definition, so it cannot be required to exist.
        tail_param = bool(params) and _PARAM_RE.fullmatch(path.rstrip("/").rsplit("/", 1)[-1])
        client_named_create = method == "POST" and tail_param
        if self.creates and not client_named_create:
            for name in params:
                v = req.path_params.get(name)
                if v is None or str(v) not in self.entities:
                    return Response(status=404, json={"message": "not found"})

        if client_named_create:
            key = str(req.path_params[params[-1]])
            self.entities[key] = {"id": req.path_params[params[-1]], **self._accepted(req)}
            return Response(status=201, json=dict(self.entities[key]))

        if method == "POST" and not path.rstrip("/").endswith("}"):
            self.counter += 1
            key = str(self.counter)
            row = {"id": self.counter, **self._accepted(req)}
            self.entities[key] = row
            return Response(status=201, json=dict(row),
                            headers={"location": f"{path}/{self.counter}"})
        if method in ("GET", "HEAD"):
            if params and self.creates:
                return Response(status=200,
                                json=dict(self.entities[str(req.path_params[params[-1]])]))
            if params:
                return Response(status=200, json={"result": 1})   # computational route
            return Response(status=200, json=[dict(v) for v in list(self.entities.values())[:5]])
        if method in ("PUT", "PATCH"):
            if not params or not self.creates:
                return Response(status=200, json={"result": 1})
            key = str(req.path_params[params[-1]])
            self.entities[key].update(self._accepted(req))
            return Response(status=200, json=dict(self.entities[key]))
        if method == "DELETE":
            if params and self.creates:
                self.entities.pop(str(req.path_params[params[-1]]), None)
                return Response(status=204)
            return Response(status=405, json={"message": "not allowed"})
        return Response(status=200, json={})


async def exercise(path: str) -> dict:
    name = path.split("/")[-1].replace("-openapi.json", "")
    row: dict = {"spec": name}

    t0 = time.monotonic()
    ops = order_operations(parse_spec(path))
    row["ops"] = len(ops)
    row["parse_s"] = time.monotonic() - t0

    # planning must terminate and respect the documented bound for every target
    t0 = time.monotonic()
    synth = SequenceSynthesizer(ops)
    longest = 0
    for op in synth.targets():
        plan = synth.synthesize(op, rkg=None)
        assert plan and plan[-1].key == op.key, f"{name}: plan must end at its target"
        assert len(plan) <= MAX_LEN, f"{name}: plan of {len(plan)} exceeds MAX_LEN"
        longest = max(longest, len(plan))
    row["plan_s"] = time.monotonic() - t0
    row["longest_plan"] = longest
    assert row["plan_s"] < PLAN_BUDGET, f"{name}: planning took {row['plan_s']:.1f}s"

    declared = {o.key: set((o.request_body.schema.get("properties") or {}).keys())
                for o in ops if o.request_body and isinstance(o.request_body.schema, dict)}
    creates = any(o.method == "POST" and not o.path.rstrip("/").endswith("}") for o in ops)
    api = GenericApi(declared, creates=creates)
    runner = TrackARunner(ops, "http://generic", budget_seconds=BUDGET, seed=1, client=api)
    res = await runner.run_async()
    row.update(seqs=res.sequences_run, reqs=api.total_requests,
               cov=res.operations_covered, faults=res.unique_5xx,
               viol=res.unique_violations, errors=res.sequence_errors,
               unrep=res.unreproduced, entities=runner.rkg.size(),
               by_oracle=dict(res.violations["by_oracle"]))
    return row


async def main() -> int:
    specs = sorted(glob.glob("specs/*-openapi.json"))
    assert specs, "no specs found; run from the tool directory"
    print(f"exercising {len(specs)} specs, {BUDGET}s of fuzzing each\n")
    print(f"{'spec':30}{'ops':>5}{'plan':>7}{'max':>5}{'seqs':>7}{'reqs':>7}"
          f"{'cov':>8}{'5xx':>5}{'viol':>5}{'err':>5}")
    rows, bad = [], []
    for path in specs:
        try:
            r = await exercise(path)
        except Exception as e:
            bad.append((path, repr(e)))
            print(f"{path.split('/')[-1][:29]:30} FAILED: {e!r}"[:110])
            continue
        rows.append(r)
        print(f"{r['spec'][:29]:30}{r['ops']:>5}{r['plan_s']:>6.2f}s{r['longest_plan']:>5}"
              f"{r['seqs']:>7}{r['reqs']:>7}{str(r['cov'])+'/'+str(r['ops']):>8}"
              f"{r['faults']:>5}{r['viol']:>5}{r['errors']:>5}")

    print(f"\n{len(rows)}/{len(specs)} specs completed")
    # This API is correct for the properties these oracles test, so any of them
    # firing is a false positive of ours. schema_conformance and
    # cross_entity_access are excluded: a generic mock cannot satisfy fifteen
    # declared response schemas, and it does not enforce parent/child ownership.
    MUST_STAY_SILENT = ("crud_roundtrip", "zombie_resource", "safety",
                        "repeated_delete", "idempotency", "mass_assignment")
    noisy = [r for r in rows
             if r["faults"] or any(r["by_oracle"].get(o) for o in MUST_STAY_SILENT)]
    stuck = [r for r in rows if r["cov"] == 0]
    erring = [r for r in rows if r["errors"] > r["seqs"] * 0.2 and r["seqs"] > 10]
    for label, group in (("report false positives against a correct API", noisy),
                         ("covered no operation at all", stuck),
                         ("errored on >20% of sequences", erring)):
        if group:
            print(f"\n!! {len(group)} spec(s) {label}: "
                  + ", ".join(r["spec"] for r in group[:6]))
    if bad:
        print(f"\n!! {len(bad)} spec(s) raised:")
        for p, e in bad:
            print(f"   {p}: {e[:120]}")
    if noisy:
        for r in noisy:
            fp = {k: v for k, v in r["by_oracle"].items() if k in MUST_STAY_SILENT}
            print(f"   {r['spec']}: 5xx={r['faults']} {fp}")
    return 1 if (bad or stuck or noisy) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
