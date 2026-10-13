"""Minimal stand-ins for the spec component's functions.

Used automatically by ``strategy.run`` when ``restleague.spec`` is not
importable yet, and by the engine's own tests. Good enough to drive the engine
end to end; the spec component's real parser/generator should replace it (same signatures).

    parse_spec(path) -> list[Operation]
    order_operations(ops) -> list[Operation]
    generate_request(op, pool, mode) -> Request
"""
from __future__ import annotations

import copy
import json
import random
import string
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from ..models import Mode, Operation, Parameter, Request, RequestBody

_rng = random.Random()
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


# ------------------------------------------------------------------ parsing
def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        return json.loads(text)
    except ValueError:
        import yaml  # type: ignore
        return yaml.safe_load(text)


def _resolve(node: Any, root: dict, seen: frozenset = frozenset(), depth: int = 0) -> Any:
    if depth > 12:
        return {}
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref = node["$ref"]
            if ref in seen or not ref.startswith("#/"):
                return {"type": "object"}
            target: Any = root
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(target, root, seen | {ref}, depth + 1)
        out = {k: _resolve(v, root, seen, depth + 1) for k, v in node.items()}
        if "allOf" in out and isinstance(out["allOf"], list):
            merged: dict = {"type": "object", "properties": {}, "required": []}
            for sub in out.pop("allOf"):
                if isinstance(sub, dict):
                    merged["properties"].update(sub.get("properties", {}))
                    merged["required"] += sub.get("required", [])
                    for k, v in sub.items():
                        if k not in ("properties", "required"):
                            merged.setdefault(k, v)
            merged["properties"].update(out.pop("properties", {}))
            merged["required"] += out.pop("required", [])
            merged.update(out)
            out = merged
        return out
    if isinstance(node, list):
        return [_resolve(x, root, seen, depth + 1) for x in node]
    return node


def parse_spec(path: str) -> list[Operation]:
    root = _load(path)
    swagger2 = str(root.get("swagger", "")).startswith("2")
    base = ""
    if swagger2:
        base = (root.get("basePath") or "").rstrip("/")
    else:
        servers = root.get("servers") or []
        if servers and isinstance(servers[0], dict):
            url = servers[0].get("url", "")
            if "://" in url:
                url = "/" + url.split("://", 1)[1].split("/", 1)[-1] if "/" in url.split("://", 1)[1] else ""
            base = url.rstrip("/") if url.startswith("/") else ""
    ops: list[Operation] = []
    for p, item in (root.get("paths") or {}).items():
        item = _resolve(item, root)
        common = item.get("parameters", [])
        for m in METHODS:
            if m not in item:
                continue
            o = item[m] or {}
            params: list[Parameter] = []
            body: Optional[RequestBody] = None
            raw_params = {(x.get("name"), x.get("in")): x for x in common if isinstance(x, dict)}
            raw_params.update({(x.get("name"), x.get("in")): x for x in o.get("parameters", []) if isinstance(x, dict)})
            form_props, form_req = {}, []
            for prm in raw_params.values():
                loc = prm.get("in")
                if loc == "body":
                    body = RequestBody(schema=prm.get("schema", {}),
                                       content_types=o.get("consumes") or root.get("consumes") or ["application/json"],
                                       required=prm.get("required", False))
                    continue
                schema = prm.get("schema") or {k: v for k, v in prm.items()
                                               if k in ("type", "format", "enum", "minimum", "maximum", "items",
                                                        "default", "pattern", "minLength", "maxLength")}
                if loc == "formData":
                    form_props[prm["name"]] = schema
                    if prm.get("required"):
                        form_req.append(prm["name"])
                params.append(Parameter(name=prm.get("name", ""), location=loc, required=bool(prm.get("required")) or loc == "path",
                                        schema=schema, style=prm.get("style"), explode=prm.get("explode")))
            if form_props and body is None:
                cts = o.get("consumes") or root.get("consumes") or ["application/x-www-form-urlencoded"]
                body = RequestBody(schema={"type": "object", "properties": form_props, "required": form_req},
                                   content_types=cts, required=bool(form_req))
                params = [x for x in params if x.location != "formData"]
            rb = o.get("requestBody")
            if isinstance(rb, dict) and rb.get("content"):
                content = rb["content"]
                cts = sorted(content, key=lambda c: 0 if "json" in c else (1 if "form" in c else 2))
                body = RequestBody(schema=(content[cts[0]] or {}).get("schema", {}), content_types=cts,
                                   required=rb.get("required", False),
                                   schemas={c: (content[c] or {}).get("schema", {}) for c in cts})
            responses = {}
            for code, r in (o.get("responses") or {}).items():
                r = r or {}
                if swagger2:
                    responses[str(code)] = r.get("schema", {}) or {}
                else:
                    c = r.get("content") or {}
                    first = next(iter(c.values()), {}) if c else {}
                    responses[str(code)] = (first or {}).get("schema", {}) or {}
            ops.append(Operation(method=m.upper(), path=base + p, operation_id=o.get("operationId", ""),
                                 parameters=params, request_body=body, responses=responses,
                                 tags=o.get("tags", []), produces=o.get("produces", [])))
    return ops


def order_operations(ops: list[Operation]) -> list[Operation]:
    """Creators first (POST, shallow paths first), then reads/updates, DELETE last."""
    rank = {"POST": 0, "PUT": 2, "GET": 1, "PATCH": 2, "HEAD": 3, "OPTIONS": 3, "DELETE": 4}

    def key(o: Operation):
        depth = o.path.count("{")
        return (rank.get(o.method, 3) if o.method != "POST" else 0, depth, o.path)

    return sorted(ops, key=key)


# --------------------------------------------------------------- generation
def _valid_string(schema: dict, name: str) -> str:
    fmt = (schema.get("format") or "").lower()
    n = name.lower()
    if fmt == "date":
        return date.today().isoformat()
    if fmt in ("date-time", "datetime"):
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if fmt == "uuid":
        return str(uuid.uuid4())
    if fmt == "email" or "email" in n:
        return f"user{_rng.randint(1, 9999)}@example.com"
    if fmt in ("uri", "url") or n.endswith("url"):
        return "https://example.com/x"
    if fmt == "byte":
        return "aGVsbG8="
    if fmt == "password" or "password" in n:
        return "Passw0rd!x"
    if "phone" in n or "telephone" in n:
        return "6085551023"
    if n in ("lang", "language", "locale"):
        return "en"
    if "currency" in n:
        return "USD"
    if "country" in n:
        return "US"
    if "date" in n:
        return date.today().isoformat()
    lo = int(schema.get("minLength", 1) or 1)
    hi = int(schema.get("maxLength", max(lo, 10)) or max(lo, 10))
    ln = max(lo, min(hi, 8))
    return "".join(_rng.choice(string.ascii_letters) for _ in range(ln))


_ALT_RE = __import__("re").compile(r"^\^?\(?([A-Za-z0-9_\- ]+(?:\|[A-Za-z0-9_\- ]+)+)\)?\$?$")


def _from_pattern(pattern: str) -> Optional[str]:
    """Pull a literal out of simple patterns like ^(cat|dog|bird)$ or ^[A-Z]{3}$."""
    m = _ALT_RE.match(pattern.strip())
    if m:
        return _rng.choice(m.group(1).split("|"))
    import re as _re
    if _re.fullmatch(r"\^?\[A-Za-z\]\{(\d+)(,\d+)?\}\$?", pattern.replace(" ", "")):
        n = int(_re.search(r"\{(\d+)", pattern).group(1))
        return "".join(_rng.choice(string.ascii_letters) for _ in range(n))
    if _re.fullmatch(r"\^?\[0-9\]\{(\d+)(,\d+)?\}\$?", pattern.replace(" ", "")):
        n = int(_re.search(r"\{(\d+)", pattern).group(1))
        return "".join(_rng.choice(string.digits) for _ in range(n))
    return None


def gen_value(schema: dict, name: str, pool, op: Optional[Operation], mode: Mode, depth: int = 0) -> Any:
    schema = schema or {}
    if depth > 5:
        return None
    if mode is Mode.VALID and isinstance(schema.get("pattern"), str):
        pv = _from_pattern(schema["pattern"])
        if pv is not None:
            return pv
    if mode is Mode.VALID and pool is not None and depth <= 2 and (name.lower().endswith("id") or name.lower() == "id"):
        v = pool.lookup(name, schema, op)
        if v is not None:
            return v
    for alt in ("oneOf", "anyOf"):
        if schema.get(alt):
            return gen_value(_rng.choice(schema[alt]), name, pool, op, mode, depth + 1)
    if mode is Mode.VALID:
        if "example" in schema and schema["example"] is not None:
            return copy.deepcopy(schema["example"])
        if "default" in schema and schema["default"] is not None:
            return copy.deepcopy(schema["default"])
    if schema.get("enum"):
        vals = [v for v in schema["enum"] if v is not None] or schema["enum"]
        return _rng.choice(vals) if mode is Mode.VALID else _rng.choice(["NOT_AN_ENUM", vals[0]])
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t is None:
        t = "object" if "properties" in schema else ("array" if "items" in schema else "string")
    if t == "object":
        props = schema.get("properties", {}) or {}
        req = set(schema.get("required", []) or [])
        out = {}
        for k, s in props.items():
            if isinstance(s, dict) and s.get("readOnly") and k not in req:
                continue
            if k in req or _rng.random() < 0.8:
                out[k] = gen_value(s, k, pool, op, mode, depth + 1)
        return out
    if t == "array":
        n = max(1, int(schema.get("minItems", 1) or 1))
        return [gen_value(schema.get("items", {}), name, pool, op, mode, depth + 1) for _ in range(n)]
    if t in ("integer", "number"):
        lo = schema.get("minimum", 1 if "id" in name.lower() else 0)
        hi = schema.get("maximum", max(lo, 0) + 100)
        if mode is Mode.BOUNDARY:
            return _rng.choice([lo, hi, lo - 1, hi + 1, 0, -1, 2**31, 2**63])
        v = _rng.randint(int(lo), int(max(lo, hi)))
        return v if t == "integer" else float(v) + 0.5
    if t == "boolean":
        return _rng.random() < 0.5
    if mode is Mode.BOUNDARY:
        return _rng.choice(["", "a" * 10000, "ü€😀", " "])
    return _valid_string(schema, name)


def generate_request(op: Operation, pool, mode: Mode = Mode.VALID) -> Request:
    req = Request(method=op.method, path=op.path, op_key=op.key)
    for p in op.parameters:
        if not p.required and _rng.random() < 0.5 and p.location != "path":
            continue
        v = None
        if pool is not None and mode is Mode.VALID:
            v = pool.lookup(p.name, p.schema, op)
        if v is None:
            v = gen_value(p.schema, p.name, pool, op, mode)
        {"path": req.path_params, "query": req.query, "header": req.headers,
         "cookie": req.cookies}.get(p.location, req.query)[p.name] = v
    if op.request_body is not None:
        ct = op.request_body.content_types[0] if op.request_body.content_types else "application/json"
        req.content_type = ct
        val = gen_value(op.request_body.schema, "body", pool, op, mode)
        if "form" in ct and isinstance(val, dict):
            req.form = val
        else:
            req.body = val
    return req
