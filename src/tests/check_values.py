"""Generate many requests for every operation of every spec, in every mode.
Fails on crashes, on non-JSON-serializable values, or on VALID values that break the schema."""
import glob
import json
import re

from restleague.models import Mode
from restleague.spec.deps import order_operations
from restleague.spec.parser import parse_spec
from restleague.spec.values import generate_request


def check_valid(value, schema, where, problems):
    if not schema or value is None:
        return
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: {value!r} not in enum")
    t = schema.get("type")
    if t == "object" and isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value and not schema["properties"].get(k, {}).get("readOnly"):
                problems.append(f"{where}: missing required {k}")
        for k, v in value.items():
            check_valid(v, schema.get("properties", {}).get(k, {}), f"{where}.{k}", problems)
    elif t == "string" and isinstance(value, str):
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    problems.append(f"{where}: {value!r} !~ {schema['pattern']}")
            except re.error:
                pass  # Java-only regex syntax such as \p{L}
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**9):
            problems.append(f"{where}: bad length {len(value)}")
    elif t in ("integer", "number") and isinstance(value, (int, float)):
        if value < schema.get("minimum", -10**30) or value > schema.get("maximum", 10**30):
            problems.append(f"{where}: {value} out of range")


total = 0
for path in sorted(glob.glob("specs/*-openapi.json")):
    ops = order_operations(parse_spec(path))
    problems = []
    for op in ops:
        for mode in Mode:
            for _ in range(30):
                req = generate_request(op, None, mode)
                assert req.method == op.method and req.path == op.path and req.op_key == op.key
                json.dumps(req.body, default=str)
                total += 1
                if mode == Mode.VALID:
                    check_valid(req.body or req.form or None, op.request_body.schema if op.request_body else {}, op.key + " body", problems)
                    for p in op.parameters:
                        for bag in (req.path_params, req.query):
                            if p.name in bag:
                                check_valid(bag[p.name], p.schema, f"{op.key} {p.name}", problems)
    uniq = sorted(set(problems))
    print(f"{'OK  ' if not uniq else 'WARN'} {path:50} problems={len(uniq)}")
    for msg in uniq[:5]:
        print("      ", msg[:150])
print(f"{total} requests generated")
