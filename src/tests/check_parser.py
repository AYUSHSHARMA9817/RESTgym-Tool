"""Run parse_spec on every spec in specs/ and print a summary.
Usage: python3 -m tests.check_parser [spec-name-filter]"""
import glob
import json
import sys
import traceback

from restleague.spec.parser import parse_spec

pattern = sys.argv[1] if len(sys.argv) > 1 else ""
for path in sorted(glob.glob("specs/*-openapi.json")):
    if pattern not in path:
        continue
    spec = json.load(open(path))
    expected = sum(1 for item in spec["paths"].values() for m in item
                   if m in ("get", "post", "put", "delete", "patch", "head", "options"))
    try:
        ops = parse_spec(path)
    except Exception:
        print(f"CRASH {path}")
        traceback.print_exc()
        continue
    leftover = sum(json.dumps(o.request_body.schema if o.request_body else {}).count("$ref")
                   + sum(json.dumps(p.schema).count("$ref") for p in o.parameters) for o in ops)
    status = "OK  " if len(ops) == expected and leftover == 0 else "FAIL"
    print(f"{status} {path:50} ops={len(ops)}/{expected} unresolved_refs={leftover}")
