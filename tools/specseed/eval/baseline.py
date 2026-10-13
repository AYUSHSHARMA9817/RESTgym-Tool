"""Baseline input generator for the evaluation: random values that follow only the
schema `type` and `enum`, the way a simple schema-based random tester does.
It ignores examples, defaults, formats, patterns, length/range limits and names."""
import random
import string
from typing import Any

from restleague.models import Mode, Operation, Request

rng = random.Random()
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def naive_value(schema: dict, depth: int = 0) -> Any:
    schema = schema or {}
    if schema.get("enum"):
        return rng.choice(schema["enum"])
    t = schema.get("type") or ("object" if "properties" in schema else
                               "array" if "items" in schema else "string")
    if t == "object":
        if depth > 6:
            return {}
        return {k: naive_value(v, depth + 1) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [] if depth > 6 else [naive_value(schema.get("items", {}), depth + 1)]
    if t == "integer":
        return rng.randint(0, 1000)
    if t == "number":
        return round(rng.uniform(0, 1000), 2)
    if t == "boolean":
        return rng.random() < 0.5
    return "".join(rng.choice(string.ascii_letters) for _ in range(8))


def naive_request(op: Operation, pool: Any = None, mode: Mode = Mode.VALID) -> Request:
    rb = op.request_body
    req = Request(method=op.method, path=op.path, op_key=op.key,
                  content_type=rb.content_types[0] if rb else None)
    for p in op.parameters:
        if not p.required and rng.random() < 0.5:
            continue
        bag = {"path": req.path_params, "query": req.query, "header": req.headers}.get(p.location)
        if bag is not None:
            v = naive_value(p.schema)
            bag[p.name] = v if p.location != "header" else str(v)
    if rb is not None and (rb.required or rng.random() < 0.8):
        body = naive_value(rb.schema)
        if req.content_type in FORM_TYPES and isinstance(body, dict):
            req.form = body
        else:
            req.body = body
    return req
