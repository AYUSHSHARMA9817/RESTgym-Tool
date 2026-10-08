"""Offline evaluation (no API needed). Writes three CSV files to the output directory:

  parser.csv       per spec: operations, $refs, allOf, recursive schemas, parse time
  conformance.csv  per spec and generator: share of VALID inputs that satisfy their schema
  cost.csv         per spec: mean time to generate one request

Usage: python3 -m eval.run_offline ../paper/data
"""
import csv
import glob
import json
import os
import re
import statistics
import sys
import time

from eval import baseline
from eval.conformance import violations
from restleague.models import Mode
from restleague.spec import values
from restleague.spec.parser import parse_spec

SPECS = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "specs", "*-openapi.json")))
REQUESTS_PER_OP = 50
SEED = 2026


def name_of(path):
    return os.path.basename(path).replace("-openapi.json", "")


def recursive_schemas(spec):
    """Component schemas that can reach themselves through $refs."""
    graph = {k: set(re.findall(r"#/components/schemas/([\w.\-]+)", json.dumps(v)))
             for k, v in spec.get("components", {}).get("schemas", {}).items()}
    count = 0
    for start in graph:
        seen, todo = set(), list(graph[start])
        while todo:
            n = todo.pop()
            if n == start:
                count += 1
                break
            if n not in seen:
                seen.add(n)
                todo += graph.get(n, ())
    return count


def inputs_of(req, op):
    """(value, schema) pairs: every sent path/query parameter, plus the body."""
    pairs = []
    for p in op.parameters:
        bag = {"path": req.path_params, "query": req.query}.get(p.location)
        if bag is not None and p.name in bag:
            pairs.append((bag[p.name], p.schema))
    if op.request_body is not None and (req.body is not None or req.form):
        pairs.append((req.body if req.body is not None else req.form, op.request_body.schema))
    return pairs


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    parser_rows, conf_rows, cost_rows = [], [], []
    kinds = {"tool": {}, "baseline": {}}  # generator -> violation kind -> count
    for path in SPECS:
        spec = json.load(open(path))
        raw = json.dumps(spec)
        times = []
        for _ in range(5):
            t = time.perf_counter()
            ops = parse_spec(path)
            times.append(time.perf_counter() - t)
        left = sum(json.dumps([o.request_body.schema if o.request_body else {},
                               [p.schema for p in o.parameters], o.responses]).count("$ref") for o in ops)
        parser_rows.append({
            "api": name_of(path), "operations": len(ops),
            "refs_in_spec": raw.count('"$ref"'), "allof_in_spec": raw.count('"allOf"'),
            "examples_in_spec": raw.count('"example"') + raw.count('"examples"'),
            "patterns_in_spec": raw.count('"pattern"'), "formats_in_spec": raw.count('"format"'),
            "recursive_schemas": recursive_schemas(spec), "refs_left_after_parse": left,
            "parse_ms_median": round(statistics.median(times) * 1000, 2),
        })

        for gen_name, gen, rng in (("tool", values.generate_request, values.rng),
                                   ("baseline", baseline.naive_request, baseline.rng)):
            rng.seed(SEED)
            ok = total = 0
            t = time.perf_counter()
            for op in ops:
                for _ in range(REQUESTS_PER_OP):
                    req = gen(op, None, Mode.VALID)
                    for value, schema in inputs_of(req, op):
                        total += 1
                        found = violations(value, schema)
                        ok += not found
                        for v in found:
                            kind = re.sub(r"^bad .*", "format", v.split(": ", 1)[1])
                            kind = "pattern" if "pattern" in kind else kind.split(" ")[0] if kind != "format" else kind
                            kinds[gen_name][kind] = kinds[gen_name].get(kind, 0) + 1
            elapsed = time.perf_counter() - t
            conf_rows.append({"api": name_of(path), "generator": gen_name, "inputs": total,
                              "conforming": ok, "share": round(ok / total, 4) if total else ""})
            if gen_name == "tool":
                cost_rows.append({"api": name_of(path), "requests": len(ops) * REQUESTS_PER_OP,
                                  "mean_us_per_request": round(elapsed / max(1, len(ops) * REQUESTS_PER_OP) * 1e6, 1)})

    kind_rows = [{"generator": g, "kind": k, "violations": n} for g in kinds for k, n in sorted(kinds[g].items())]
    kind_rows = kind_rows or [{"generator": "", "kind": "", "violations": 0}]
    for fname, rows in (("parser.csv", parser_rows), ("conformance.csv", conf_rows), ("cost.csv", cost_rows),
                        ("violations.csv", kind_rows)):
        with open(os.path.join(out_dir, fname), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {fname}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out")
