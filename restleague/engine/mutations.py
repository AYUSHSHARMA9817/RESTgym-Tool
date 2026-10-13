"""Mutation operators applied to known-good requests.

Each operator: ``fn(req, op, pool, rng) -> Request | None`` (None = not
applicable to this request). Operators work on a deep copy and set
``req.mutation`` to their name, so faults can be attributed to them.
"""
from __future__ import annotations

import copy
import json
import random
from typing import Any, Callable, Iterator, Optional

from ..models import Operation, Request

Slot = tuple  # ("path"|"query"|"header"|"cookie"|"form", name) or ("body", *keys)

HUGE_INTS = [0, -1, -2**31, 2**31 - 1, 2**31, 2**53 + 1, 2**63 - 1, 2**63, -2**63 - 1, 10**30]
HUGE_FLOATS = [1e308, -1e308, 1e-320, 0.1, -0.0, 3.4028235e39]

# Vulnerability-detection probes. These are *detection* inputs — the same class
# of strings OWASP ZAP / Burp / sqlmap / schemathesis send — used only to reveal
# whether a target API mishandles them (a 5XX, an unescaped reflection, a stack
# trace). They are run against the competition's intentionally-vulnerable
# benchmark APIs inside the organizers' sandbox.
SQLI = [
    "' OR '1'='1", "' OR 1=1--", "'; DROP TABLE users;--", "1' AND SLEEP(3)--",
    "\" OR \"\"=\"", "') OR ('1'='1", "1 UNION SELECT NULL--", "admin'--", "' OR '1'='1' /*",
    "1;SELECT pg_sleep(3)", "%27%20OR%201%3D1--",
]
NOSQLI = ['{"$gt": ""}', '{"$ne": null}', "[$ne]=1", '{"$where": "sleep(3000)"}', "true, $where: '1 == 1'"]
XSS = [
    "<script>alert(1)</script>", "\"><script>alert(1)</script>", "<img src=x onerror=alert(1)>",
    "javascript:alert(1)", "<svg/onload=alert(1)>", "'\"><svg onload=alert(1)>",
]
TEMPLATE_INJECTION = ["{{7*7}}", "${7*7}", "#{7*7}", "<%= 7*7 %>", "${{7*7}}", "{{config}}", "@(7*7)", "%{7*7}"]
PATH_TRAVERSAL = [
    "../../../../etc/passwd", "..\\..\\..\\..\\windows\\win.ini", "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "....//....//etc/passwd", "/etc/passwd%00.png", "file:///etc/passwd",
]
COMMAND_INJECTION = ["; sleep 3", "| sleep 3", "`sleep 3`", "$(sleep 3)", "& ping -c 3 127.0.0.1", "\n/bin/true"]
HEADER_INJECTION = ["a\r\nX-Injected: 1", "a%0d%0aSet-Cookie:x=1", "a\nSet-Cookie: s=1"]
XXE = ['<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]><r>&e;</r>']
FORMAT_STRING = ["%s%s%s%s%s", "%n%n%n", "%x%x%x%x", "{0}{1}{2}", "%(x)s"]

INJECTION_PAYLOADS = (SQLI + NOSQLI + XSS + TEMPLATE_INJECTION + PATH_TRAVERSAL +
                      COMMAND_INJECTION + HEADER_INJECTION + XXE + FORMAT_STRING)

SPECIAL_STRINGS = [
    "", " ", "\t\n\r", "\u0000", "\\", "%", "%%", "%00", "%n", "ü€😀𝕏", "١٢٣", "null", "NaN",
    "Infinity", "-1", "0", "true", "[]", "{}", "a" * 300, "1e999", "0x7fffffff", ":", "/", "?",
    "#", "&=", "2021-02-30", "9999-12-31T23:59:59Z", "not-a-date",
] + INJECTION_PAYLOADS
BAD_IDS = [0, -1, 999999999, 2**31, 2**63, -2**63 - 1, "abc", "1.5", "1e3", " ", "null", "%20",
           "00000000-0000-0000-0000-000000000000", "a" * 200, "..", "%2e%2e", "../1", "' OR 1=1--",
           "1 OR 1=1", "0;SELECT 1"]
INVALID_FORMAT = {
    "date": ["2021-02-30", "2021-13-01", "01/02/2021", "today", "0000-00-00", "99999-01-01"],
    "date-time": ["2021-02-30T25:61:61Z", "yesterday", "2021-01-01", "1611111111", "2021-01-01T00:00:00+99:00"],
    "uuid": ["not-a-uuid", "1234", "00000000-0000-0000-0000-00000000000Z"],
    "email": ["plainaddress", "@@", "a@", "a" * 300 + "@x.com"],
    "uri": ["ht!tp://", "::::", "not a uri", "javascript:alert(1)", "file:///etc/passwd"],
    "int32": [2**31, -2**31 - 1],
    "int64": [2**63, -2**63 - 1],
}


# ------------------------------------------------------------ slot helpers
def _body_paths(node: Any, prefix: tuple = (), depth: int = 0) -> Iterator[tuple]:
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield prefix + (k,)
            yield from _body_paths(v, prefix + (k,), depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):
            yield prefix + (i,)
            yield from _body_paths(v, prefix + (i,), depth + 1)


def slots(req: Request, include_path: bool = True) -> list[Slot]:
    out: list[Slot] = []
    if include_path:
        out += [("path", k) for k in req.path_params]
    out += [("query", k) for k in req.query]
    out += [("header", k) for k in req.headers]
    out += [("cookie", k) for k in req.cookies]
    out += [("form", k) for k in req.form]
    if req.raw_body is None and req.body is not None:
        out += [("body",) + p for p in _body_paths(req.body)]
    return out


def _container(req: Request, slot: Slot):
    kind = slot[0]
    if kind == "body":
        node = req.body
        for k in slot[1:-1]:
            node = node[k]
        return node, slot[-1]
    return {"path": req.path_params, "query": req.query, "header": req.headers,
            "cookie": req.cookies, "form": req.form}[kind], slot[1]


def get_slot(req: Request, slot: Slot) -> Any:
    c, k = _container(req, slot)
    return c[k]


def set_slot(req: Request, slot: Slot, value: Any) -> None:
    c, k = _container(req, slot)
    c[k] = value


def del_slot(req: Request, slot: Slot) -> None:
    c, k = _container(req, slot)
    if isinstance(c, dict):
        c.pop(k, None)
    elif isinstance(c, list) and isinstance(k, int) and k < len(c):
        c.pop(k)


def schema_for(op: Operation, slot: Slot) -> dict:
    kind = slot[0]
    if kind in ("path", "query", "header", "cookie"):
        for p in op.parameters:
            if p.name == slot[1] and p.location == kind:
                return p.schema or {}
        return {}
    s = (op.request_body.schema if op.request_body else {}) or {}
    keys = slot[1:] if kind == "body" else slot[1:]
    for k in keys:
        if not isinstance(s, dict):
            return {}
        if isinstance(k, int):
            s = s.get("items", {})
        else:
            s = (s.get("properties") or {}).get(k, {})
    return s if isinstance(s, dict) else {}


def _required_slots(req: Request, op: Operation) -> list[Slot]:
    out: list[Slot] = []
    for p in op.parameters:
        if p.required and p.location in ("query", "header", "cookie") and p.name in getattr(req, {"query": "query", "header": "headers", "cookie": "cookies"}[p.location]):
            out.append((p.location, p.name))
    if op.request_body and isinstance(req.body, dict):
        for k in (op.request_body.schema or {}).get("required", []) or []:
            if k in req.body:
                out.append(("body", k))
    if op.request_body and req.form:
        for k in (op.request_body.schema or {}).get("required", []) or []:
            if k in req.form:
                out.append(("form", k))
    return out


def _clone(req: Request, name: str) -> Request:
    r = copy.deepcopy(req)
    r.mutation = name
    return r


def _pick(rng: random.Random, seq):
    return rng.choice(seq) if seq else None


# --------------------------------------------------------------- operators
def drop_required(req, op, pool, rng):
    cand = _required_slots(req, op)
    if not cand:
        return None
    r = _clone(req, "drop_required")
    for s in rng.sample(cand, k=min(len(cand), rng.choice([1, 1, 2, len(cand)]))):
        del_slot(r, s)
    return r


def drop_random(req, op, pool, rng):
    cand = slots(req, include_path=False)
    if not cand:
        return None
    r = _clone(req, "drop_random")
    try:
        del_slot(r, rng.choice(cand))
    except (KeyError, IndexError, TypeError):
        return None
    return r


def null_field(req, op, pool, rng):
    cand = slots(req, include_path=False)
    if not cand:
        return None
    r = _clone(req, "null_field")
    try:
        set_slot(r, rng.choice(cand), None)
    except (KeyError, IndexError, TypeError):
        return None
    return r


def _confuse(v: Any, rng: random.Random) -> Any:
    if isinstance(v, bool):
        return rng.choice(["maybe", 2, "TRUE", None])
    if isinstance(v, int):
        return rng.choice(["abc", str(v) + "x", 1.5, [v], {"v": v}, True])
    if isinstance(v, float):
        return rng.choice(["1,5", "NaN", [v], True])
    if isinstance(v, str):
        return rng.choice([12345, -1, 1.5, True, [v], {"value": v}])
    if isinstance(v, list):
        return rng.choice([{"0": v[0] if v else 1}, "x", 1, [[v]]])
    if isinstance(v, dict):
        return rng.choice([[v], "x", 1, True])
    return rng.choice(["x", 1, [], {}])


def type_confusion(req, op, pool, rng):
    cand = slots(req)
    if not cand:
        return None
    r = _clone(req, "type_confusion")
    s = rng.choice(cand)
    try:
        set_slot(r, s, _confuse(get_slot(r, s), rng))
    except (KeyError, IndexError, TypeError):
        return None
    return r


def boundary_number(req, op, pool, rng):
    cand = []
    for s in slots(req):
        try:
            v = get_slot(req, s)
        except Exception:
            continue
        sch = schema_for(op, s)
        if (isinstance(v, (int, float)) and not isinstance(v, bool)) or sch.get("type") in ("integer", "number"):
            cand.append((s, sch))
    if not cand:
        return None
    s, sch = rng.choice(cand)
    vals = list(HUGE_INTS)
    for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if isinstance(sch.get(k), (int, float)) and not isinstance(sch.get(k), bool):
            vals += [sch[k] - 1, sch[k] + 1, sch[k]]
    if sch.get("type") == "number":
        vals += HUGE_FLOATS
    r = _clone(req, "boundary_number")
    set_slot(r, s, rng.choice(vals))
    return r


def string_attack(req, op, pool, rng):
    cand = [s for s in slots(req) if isinstance(_safe_get(req, s), str) or schema_for(op, s).get("type") == "string"]
    if not cand:
        cand = slots(req)
    if not cand:
        return None
    s = rng.choice(cand)
    sch = schema_for(op, s)
    choices = list(SPECIAL_STRINGS) + ["", "a" * rng.choice([1000, 10000, 100000])]
    if isinstance(sch.get("maxLength"), int):
        choices += ["a" * (sch["maxLength"] + 1)] * 3
    if isinstance(sch.get("minLength"), int) and sch["minLength"] > 0:
        choices += ["a" * (sch["minLength"] - 1)] * 3
    fmt = (sch.get("format") or "").lower()
    if fmt in INVALID_FORMAT:
        choices += INVALID_FORMAT[fmt] * 3
    r = _clone(req, "string_attack")
    set_slot(r, s, rng.choice(choices))
    return r


def injection(req, op, pool, rng):
    """Place a vulnerability-detection payload (SQLi/XSS/traversal/SSTI/etc.) into a
    string-typed slot. Prefers body/query/path string fields; crashes and reflected
    payloads are how the tool flags injection points in the benchmark APIs."""
    cand = [s for s in slots(req)
            if isinstance(_safe_get(req, s), str) or schema_for(op, s).get("type") in (None, "string")]
    if not cand:
        cand = slots(req)
    if not cand:
        return None
    r = _clone(req, "injection")
    # pick a payload family, then a member — keeps families represented evenly
    family = rng.choice([SQLI, XSS, TEMPLATE_INJECTION, PATH_TRAVERSAL, COMMAND_INJECTION,
                         NOSQLI, HEADER_INJECTION, FORMAT_STRING, XXE])
    payload = rng.choice(family)
    # also consider declared query/header params that are absent from this request:
    # injection points are frequently optional filters/search params.
    absent = [("query", p.name) for p in op.params_in("query") if p.name not in r.query]
    absent += [("header", p.name) for p in op.params_in("header") if p.name not in r.headers]
    if absent and rng.random() < 0.5:
        kind, name = rng.choice(absent)
        (r.query if kind == "query" else r.headers)[name] = payload
        return r
    s = rng.choice(cand)
    base = _safe_get(req, s)
    # sometimes append to a valid value (breaks out of context), sometimes replace
    if isinstance(base, str) and base and rng.random() < 0.4:
        payload = base + payload
    set_slot(r, s, payload)
    return r


def injection_raw_body(req, op, pool, rng):
    """Send an XXE/JSON-with-injection raw body (for XML-accepting or lax endpoints)."""
    if op.request_body is None and req.method.upper() not in ("POST", "PUT", "PATCH"):
        return None
    r = _clone(req, "injection_raw_body")
    choice = rng.choice(XXE + ['{"q": "' + rng.choice(SQLI).replace('"', '\\"') + '"}',
                               '{"name": "' + rng.choice(XSS).replace('"', '\\"') + '"}'])
    r.raw_body = choice.encode("utf-8")
    r.content_type = "application/xml" if choice.startswith("<?xml") else (req.content_type or "application/json")
    return r


def invalid_enum(req, op, pool, rng):
    cand = [s for s in slots(req) if schema_for(op, s).get("enum")]
    if not cand:
        return None
    s = rng.choice(cand)
    v = _safe_get(req, s)
    r = _clone(req, "invalid_enum")
    set_slot(r, s, rng.choice(["INVALID_ENUM", "", str(v).lower() if isinstance(v, str) else "x",
                               str(v).upper() + "_" if v is not None else "X", 99]))
    return r


def invalid_format(req, op, pool, rng):
    cand = [(s, (schema_for(op, s).get("format") or "").lower()) for s in slots(req)]
    cand = [(s, f) for s, f in cand if f in INVALID_FORMAT]
    if not cand:
        return None
    s, f = rng.choice(cand)
    r = _clone(req, "invalid_format")
    set_slot(r, s, rng.choice(INVALID_FORMAT[f]))
    return r


def bad_id(req, op, pool, rng):
    cand = [("path", k) for k in req.path_params]
    cand += [s for s in slots(req, include_path=False) if str(s[-1]).lower().endswith("id")]
    if not cand:
        return None
    r = _clone(req, "bad_id")
    s = rng.choice(cand)
    val = rng.choice(BAD_IDS)
    if pool is not None and rng.random() < 0.3:
        dead = pool.dead_value() if hasattr(pool, "dead_value") else None
        other = pool.any_value() if dead is None else dead
        if other is not None:
            val = other
    set_slot(r, s, val)
    return r


def malformed_body(req, op, pool, rng):
    if op.request_body is None and req.method.upper() not in ("POST", "PUT", "PATCH"):
        return None
    good = json.dumps(req.body if req.body is not None else {}, default=str)
    variants = [
        good[:-1] if len(good) > 1 else "{",
        good + "}",
        good.replace('"', "'"),
        good.replace(":", "=", 1),
        "{" * 2000 + "}" * 2000,
        "[" * 5000,
        '{"a":1,"a":2}',
        "\x00\xff",
        "nul",
        "NaN",
        good.replace("{", "{,", 1),
        good.encode("utf-16").decode("latin-1"),
        "",
    ]
    r = _clone(req, "malformed_body")
    r.raw_body = rng.choice(variants).encode("utf-8", "surrogatepass")
    r.content_type = req.content_type or "application/json"
    return r


def wrong_content_type(req, op, pool, rng):
    if req.body is None and not req.form and op.request_body is None:
        return None
    r = _clone(req, "wrong_content_type")
    ct = rng.choice(["text/plain", "application/xml", "application/x-www-form-urlencoded",
                     "multipart/form-data; boundary=zzz", "application/octet-stream", "application/json; charset=ebcdic",
                     "*/*", "application/json-patch+json", "foo"])
    payload = req.body if req.body is not None else req.form
    if rng.random() < 0.5:
        r.raw_body = json.dumps(payload, default=str).encode()
    else:
        r.raw_body = b"<a><b>1</b></a>" if "xml" in ct else b"a=1&b=2"
    r.content_type = ct
    return r


def body_shape(req, op, pool, rng):
    if op.request_body is None:
        return None
    r = _clone(req, "body_shape")
    b = req.body
    r.body = rng.choice([
        None, {}, [], [b], [b, b], "string", 0, True,
        {"wrapper": b},
        _deep(rng.choice([50, 200, 1000])),
        {**(b if isinstance(b, dict) else {}), **{f"extra{i}": "x" for i in range(5)},
         "__proto__": {"x": 1}, "id": rng.choice(HUGE_INTS)},
    ])
    if r.body is None:
        r.raw_body = b""
    r.form = {}
    r.content_type = req.content_type or "application/json"
    return r


def _deep(n: int) -> Any:
    v: Any = 1
    for i in range(n):
        v = {"a": v} if i % 2 else [v]
    return v


def array_size(req, op, pool, rng):
    cand = [s for s in slots(req) if isinstance(_safe_get(req, s), list) or schema_for(op, s).get("type") == "array"]
    if not cand:
        return None
    s = rng.choice(cand)
    v = _safe_get(req, s)
    item = v[0] if isinstance(v, list) and v else 1
    r = _clone(req, "array_size")
    set_slot(r, s, rng.choice([[], [None], [item] * 1000, [item, item], [[item]], [item, "x", 1, None]]))
    return r


def query_pollution(req, op, pool, rng):
    r = _clone(req, "query_pollution")
    if r.query and rng.random() < 0.6:
        k = rng.choice(list(r.query))
        v = r.query[k]
        r.query[k] = [v, rng.choice(["x", -1, "", v])]  # repeated key
    else:
        r.query[rng.choice(["page", "size", "limit", "offset", "sort", "q", "filter", "fields", "order"])] = \
            rng.choice([-1, 0, 2**31, "abc", "", "id,desc", "nonexistent,asc"])
    return r


def swap_id_from_pool(req, op, pool, rng):
    """Use a real id harvested for a *different* resource."""
    if pool is None or not req.path_params:
        return None
    r = _clone(req, "swap_id")
    k = rng.choice(list(r.path_params))
    v = pool.any_value()
    if v is None or v == r.path_params[k]:
        return None
    r.path_params[k] = v
    return r


def _safe_get(req, s):
    try:
        return get_slot(req, s)
    except Exception:
        return None


OPERATORS: dict[str, Callable[..., Optional[Request]]] = {
    "drop_required": drop_required,
    "drop_random": drop_random,
    "null_field": null_field,
    "type_confusion": type_confusion,
    "boundary_number": boundary_number,
    "string_attack": string_attack,
    "invalid_enum": invalid_enum,
    "invalid_format": invalid_format,
    "bad_id": bad_id,
    "malformed_body": malformed_body,
    "wrong_content_type": wrong_content_type,
    "body_shape": body_shape,
    "array_size": array_size,
    "query_pollution": query_pollution,
    "swap_id": swap_id_from_pool,
    "injection": injection,
    "injection_raw_body": injection_raw_body,
}


# One representative payload per family — enough to flag an injection point
# without a full product blow-up. The per-slot sweep sends all of these.
SWEEP_FAMILIES = {
    "sqli": SQLI[:4], "xss": XSS[:3], "ssti": TEMPLATE_INJECTION[:3], "traversal": PATH_TRAVERSAL[:3],
    "cmdi": COMMAND_INJECTION[:3], "nosqli": NOSQLI[:3], "crlf": HEADER_INJECTION[:2],
    "fmt": FORMAT_STRING[:2], "overflow": ["a" * 100000], "nullbyte": ["x%00.png"],
}
SWEEP_PAYLOADS = [(fam, p) for fam, ps in SWEEP_FAMILIES.items() for p in ps]


def injectable_slots(req: Request, op: Operation) -> list[Slot]:
    """String-typed slots plus declared-but-absent query/header params (common
    injection points), suitable for a systematic payload sweep."""
    out: list[Slot] = []
    for s in slots(req):
        v = _safe_get(req, s)
        if isinstance(v, str) or schema_for(op, s).get("type") in (None, "string"):
            out.append(s)
    for p in op.params_in("query"):
        if p.name not in req.query:
            out.append(("query", p.name))
    for p in op.params_in("header"):
        if p.name not in req.headers:
            out.append(("header", p.name))
    return out


def sweep_request(base: Request, op: Operation, slot: Slot, payload: str, family: str) -> Optional[Request]:
    """Deep-copy ``base`` and place ``payload`` into ``slot`` (adding it if absent)."""
    r = copy.deepcopy(base)
    r.mutation = f"sweep:{family}"
    try:
        kind = slot[0]
        if kind in ("query", "header", "cookie", "form") and slot[1] not in getattr(
                r, {"query": "query", "header": "headers", "cookie": "cookies", "form": "form"}[kind]):
            getattr(r, {"query": "query", "header": "headers", "cookie": "cookies", "form": "form"}[kind])[slot[1]] = payload
        else:
            set_slot(r, slot, payload)
    except Exception:
        return None
    return r


def mutate(name: str, req: Request, op: Operation, pool, rng: random.Random, stack: int = 1) -> Optional[Request]:
    """Apply operator ``name`` (and optionally ``stack-1`` extra random ones)."""
    fn = OPERATORS[name]
    try:
        out = fn(req, op, pool, rng)
        for _ in range(stack - 1):
            if out is None:
                break
            extra = rng.choice(list(OPERATORS.values()))
            nxt = extra(out, op, pool, rng)
            if nxt is not None:
                nxt.mutation = f"{out.mutation}+{nxt.mutation}"
                out = nxt
        return out
    except Exception:
        return None
