"""Live evaluation against running APIs. For each API, configuration and repetition the API
is restarted from a fresh state, then the same request budget is spent:

  budget        : 20 requests per operation in total, the same for every configuration
  nominal phase : 2 passes over the operations; each operation gets up to 5 VALID
                  requests per pass, stopping at its first 2XX
  fuzz phase    : the rest of the budget, round-robin over the operations
                  (tool: BOUNDARY/INVALID alternating; baseline: its usual random values)

Configurations (each adds one part of the spec component):
  baseline     type/enum-only random values, spec order, no id reuse
  values       our value generator,           spec order, no id reuse
  values+pool  our value generator,           spec order, ids reused through the pool
  full         our value generator + pool + dependency-based operation order

Usage: python3 -m eval.run_live OUT_DIR [REPETITIONS] [API ...]
Writes OUT_DIR/live/<api>/<config>-<rep>.json and OUT_DIR/live.csv
"""
import csv
import json
import os
import random
import re
import sys
import time

from eval import apis, baseline
from eval.http import send
from eval.pool import SimplePool
from restleague.models import Mode
from restleague.spec import values
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec

SPEC_DIR = os.path.join(os.path.dirname(__file__), "..", "specs")
CONFIGS = ["baseline", "values", "values+pool", "full"]
PASSES, ATTEMPTS, BUDGET_PER_OP = 2, 5, 20


def fault_signature(op_key: str, status: int, text: str) -> str:
    """Unique 5XX = same operation, status and error message once numbers/ids are removed."""
    msg = text
    try:
        body = json.loads(text)
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("detail") or body.get("error") or text)
    except ValueError:
        pass
    # APIs echo the bad input inside quotes; mask it (this also masks quoted names,
    # so two messages that differ only in a quoted name count as one fault)
    msg = re.sub(r'"[^"]*"', '"<s>"', msg)
    msg = re.sub(r"'[^']*'", "'<s>'", msg)
    msg = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", msg)
    msg = re.sub(r"\d+", "<n>", msg)[:200]
    return f"{op_key}|{status}|{msg}"


def run_once(api: str, config: str, seed: int) -> dict:
    base_url = apis.start(api)
    ops = parse_spec(os.path.join(SPEC_DIR, f"{api}-openapi.json"))
    if config == "full":
        ops = order_operations(ops)
    rng = random.Random(seed)
    values.rng.seed(seed)
    baseline.rng.seed(seed)
    pool = SimplePool(rng) if config in ("values+pool", "full") else None
    gen = baseline.naive_request if config == "baseline" else values.generate_request

    per_op = {op.key: {"requests": 0, "2xx": 0, "4xx": 0, "5xx": 0, "first_2xx_at": None} for op in ops}
    faults: set[str] = set()
    sent = 0
    start = time.perf_counter()

    def fire(op, mode):
        nonlocal sent
        req = gen(op, pool, mode)
        resp = send(base_url, req)
        sent += 1
        stats = per_op[op.key]
        stats["requests"] += 1
        if resp.ok:
            stats["2xx"] += 1
            if stats["first_2xx_at"] is None:
                stats["first_2xx_at"] = sent
            if pool is not None:
                pool.record(op, resp)
        elif 400 <= resp.status < 500:
            stats["4xx"] += 1
        elif resp.is_5xx:
            stats["5xx"] += 1
            faults.add(fault_signature(op.key, resp.status, resp.text))
        return resp

    for _ in range(PASSES):
        for op in ops:
            if per_op[op.key]["2xx"]:
                continue
            for _ in range(ATTEMPTS):
                if fire(op, Mode.VALID).ok:
                    break
    nominal_requests = sent
    budget = BUDGET_PER_OP * len(ops)
    i = 0
    while sent < budget:
        fire(ops[i % len(ops)], Mode.BOUNDARY if (i // len(ops)) % 2 == 0 else Mode.INVALID)
        i += 1

    apis.stop(api)
    return {
        "api": api, "config": config, "seed": seed, "operations": len(ops),
        "ops_with_2xx": sum(1 for s in per_op.values() if s["2xx"]),
        "nominal_requests": nominal_requests, "total_requests": sent,
        "unique_5xx": len(faults), "seconds": round(time.perf_counter() - start, 1),
        "per_op": per_op, "faults": sorted(faults),
    }


def main(out_dir: str, reps: int, api_names: list[str]) -> None:
    rows = []
    for api in api_names:
        os.makedirs(os.path.join(out_dir, "live", api), exist_ok=True)
        for rep in range(reps):
            for config in CONFIGS:
                path = os.path.join(out_dir, "live", api, f"{config}-{rep}.json")
                if os.path.exists(path):  # resume: keep runs that already finished
                    with open(path) as f:
                        result = json.load(f)
                else:
                    result = run_once(api, config, seed=1000 + rep)
                    with open(path, "w") as f:
                        json.dump(result, f, indent=1)
                row = {k: v for k, v in result.items() if k not in ("per_op", "faults")}
                rows.append(row)
                print(row, flush=True)
    with open(os.path.join(out_dir, "live.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "out"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    names = sys.argv[3:] or list(apis.APIS)
    main(out, n, names)
