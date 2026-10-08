"""Owner: A. Schema -> concrete request values.

VALID    : values that should pass the API's validation (aim: 2XX, operation coverage).
BOUNDARY : structurally valid, but values at the edges of their constraints.
INVALID  : a valid request with ONE location (a parameter or a body field) broken,
           so the request gets past most validation and reaches deeper code (aim: 5XX).
"""
import random
import re
import string
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from restleague.models import Mode, Operation, Request

rng = random.Random()

FIRST_NAMES = ["Alice", "Bob", "Carlos", "Diana", "Emma", "Farid", "Grace", "Hiro"]
LAST_NAMES = ["Smith", "Garcia", "Rossi", "Kumar", "Chen", "Muller", "Silva", "Novak"]
CITIES = ["Verona", "Dublin", "Madison", "Paris", "Tokyo", "Lisbon"]
WORDS = ["alpha", "beta", "gamma", "delta", "omega", "sigma", "kappa", "zeta"]

# Inputs that commonly break parsers, converters and SQL/JSON handling.
NASTY_STRINGS = ["", " ", "null", "-1", "0", "9" * 30, "1e309", "NaN", "'", "\"", "\\", "%", "%s%n",
                 "' OR '1'='1", "<script>", "../../etc/passwd", "\u0000", "😀" * 10, "a" * 10000,
                 "2020-13-45", "not-a-uuid", "{}", "[]"]
NASTY_NUMBERS = [-1, 0, 2**31, -2**31 - 1, 2**63, -2**63 - 1, 1e308, -1e308, 0.5]

FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
MAX_STRING = 5000   # cap for maxLength-driven strings (some specs say maxLength 2^31)
MAX_ITEMS = 50


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate_request(op: Operation, pool: Any = None, mode: Mode = Mode.VALID) -> Request:
    """`pool` is B's ResourcePool (or None): pool.lookup(name, schema, op) -> real value or None."""
    rb = op.request_body
    req = Request(method=op.method, path=op.path, op_key=op.key,
                  content_type=rb.content_types[0] if rb else None)
    leaf_mode = Mode.BOUNDARY if mode == Mode.BOUNDARY else Mode.VALID

    for p in op.parameters:
        # always send required params; optional ones about half the time
        if not p.required and rng.random() < 0.5:
            continue
        target = _param_bag(req, p.location)
        if target is None:
            continue
        value = None
        if p.location in ("path", "query"):
            value = _lookup(pool, p.name, p.schema, op)
        if value is None:
            value = gen_value(p.schema, leaf_mode, p.name, pool, op=op)
        target[p.name] = value if p.location in ("path", "query") else serialize_param(value)

    if rb is not None and (rb.required or rng.random() < 0.8):
        body = gen_value(rb.schema, leaf_mode, "", pool, op=op)
        if req.content_type in FORM_TYPES and isinstance(body, dict):
            req.form = body
        else:
            req.body = body

    _align_body_id(req, op)
    if mode == Mode.INVALID:
        _break_one_location(req, op)
    return req


def _align_body_id(req: Request, op: Operation) -> None:
    """In PUT/PATCH /things/{thingId}, a body field "id" names the thing being updated:
    give it the same value as the last path parameter."""
    if op.method not in ("PUT", "PATCH") or not isinstance(req.body, dict) or "id" not in req.body:
        return
    last = op.path.rstrip("/").rsplit("/", 1)[-1]
    if last.startswith("{") and last[1:-1] in req.path_params:
        req.body["id"] = req.path_params[last[1:-1]]


def gen_value(schema: dict, mode: Mode = Mode.VALID, name: str = "",
              pool: Any = None, depth: int = 0, op: Optional[Operation] = None) -> Any:
    """A value for `schema`. `name` is the parameter/property name, used for heuristics."""
    schema = schema or {}

    if mode == Mode.VALID:
        for key in ("example", "default"):
            if key in schema and schema[key] is not None and rng.random() < 0.8:
                return schema[key]
        if schema.get("examples") and isinstance(schema["examples"], list):
            return rng.choice(schema["examples"])
    if schema.get("enum"):
        return rng.choice(schema["enum"])

    kind = _type_of(schema)
    if kind == "object":
        return _gen_object(schema, mode, pool, depth, op)
    if kind == "array":
        return _gen_array(schema, mode, name, pool, depth, op)
    if kind == "integer":
        return _gen_number(schema, mode, name, integer=True)
    if kind == "number":
        return _gen_number(schema, mode, name, integer=False)
    if kind == "boolean":
        return rng.random() < 0.5
    return _gen_string(schema, mode, name)


def is_id_like(name: str) -> bool:
    n = name.lower()
    return n == "id" or n.endswith("id") or n.endswith("_id") or n.endswith("uuid")


def serialize_param(value: Any) -> str:
    """How to put a value into a path/query/header string (used by the engine too)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(serialize_param(v) for v in value)
    if isinstance(value, dict):
        import json
        return json.dumps(value)
    if value is None:
        return ""
    return str(value)


def invalid_value(schema: dict, name: str = "") -> Any:
    """A value that violates `schema` (wrong type, out of range, malformed format...)."""
    schema = schema or {}
    kind = _type_of(schema)
    options: list[Any] = [None, rng.choice(NASTY_STRINGS)]
    if kind in ("integer", "number"):
        options += [rng.choice(NASTY_NUMBERS), "abc", True, 1.5 if kind == "integer" else "1,5"]
        if "minimum" in schema:
            options.append(schema["minimum"] - 1)
        if "maximum" in schema:
            options.append(schema["maximum"] + 1)
    elif kind == "string":
        options += [12345, True, [], {}]
        if "maxLength" in schema:
            options.append("x" * (schema["maxLength"] + 1))
        if schema.get("minLength", 0) > 0:
            options.append("x" * (schema["minLength"] - 1))
        if schema.get("format") in ("date", "date-time", "uuid", "email", "uri"):
            options += ["2021-02-30", "yesterday", "zzzz", "@", "::"]
        if schema.get("enum"):
            options.append(str(schema["enum"][0]) + "_X")
    elif kind == "boolean":
        options += ["maybe", 2, "TRUE"]
    elif kind == "array":
        options += [{}, "notalist", [None], [rng.choice(NASTY_STRINGS)] * 3]
    elif kind == "object":
        options += ["notanobject", [], {"unexpected": rng.choice(NASTY_STRINGS)}]
    return rng.choice(options)


# ---------------------------------------------------------------------------
# Generators per type
# ---------------------------------------------------------------------------
def _type_of(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # OpenAPI 3.1 style ["string", "null"]
        t = next((x for x in t if x != "null"), "string")
    if t:
        return t
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return "string"


def _gen_object(schema: dict, mode: Mode, pool, depth: int, op) -> dict:
    if depth > 6:
        return {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    out = {}
    creating = op is not None and op.method == "POST" and depth == 0
    for prop, sub in props.items():
        if sub.get("readOnly"):
            continue
        if creating and prop.lower() == "id" and prop not in required:
            continue  # the server assigns ids; many specs forget to mark them readOnly
        if prop not in required and depth > 0 and rng.random() < 0.3:
            continue  # sometimes leave out optional nested fields
        value = None
        if is_id_like(prop) and prop.lower() != "id":  # a bare "id" names no other resource
            value = _lookup(pool, prop, sub, op)
        elif _type_of(sub) == "object" and "id" in sub.get("properties", {}):
            # a reference to another resource, e.g. pet.type = {"id": 1, "name": "cat"}
            value = _lookup_object(pool, prop)
        out[prop] = value if value is not None else gen_value(sub, mode, prop, pool, depth + 1, op)
    extra = schema.get("additionalProperties")
    if not props and isinstance(extra, dict):
        out[rng.choice(WORDS)] = gen_value(extra, mode, "", pool, depth + 1, op)
    return out


def _gen_array(schema: dict, mode: Mode, name: str, pool, depth: int, op) -> list:
    lo = min(schema.get("minItems", 0), MAX_ITEMS)
    hi = min(schema.get("maxItems", lo + 2), MAX_ITEMS)
    if mode == Mode.BOUNDARY:
        count = rng.choice([lo, hi])
    else:
        count = rng.randint(max(lo, 1), max(lo, 1, min(hi, 3)))
    if depth > 6:
        count = 0
    item_name = name[:-1] if name.endswith("s") else name
    items = [gen_value(schema.get("items", {}), mode, item_name, pool, depth + 1, op) for _ in range(count)]
    if schema.get("uniqueItems"):
        unique = []
        for v in items:
            if v not in unique:
                unique.append(v)
        items = unique
    return items


def _gen_number(schema: dict, mode: Mode, name: str, integer: bool):
    lo = schema.get("minimum")
    hi = schema.get("maximum")
    if schema.get("exclusiveMinimum") is True and lo is not None:
        lo = lo + (1 if integer else 1e-6)
    if schema.get("exclusiveMaximum") is True and hi is not None:
        hi = hi - (1 if integer else 1e-6)
    if mode == Mode.BOUNDARY:
        edges = [v for v in (lo, hi) if v is not None] or [0, -1, 2**31 - 1, -2**31]
        value = rng.choice(edges)
        return int(value) if integer else value

    if lo is None and hi is None:
        lname = name.lower()
        if lname.endswith("id") or lname == "id":
            lo, hi = 1, 10
        elif any(w in lname for w in ("age", "year")):
            lo, hi = (1, 90) if "age" in lname else (1990, 2025)
        elif any(w in lname for w in ("page", "offset")):
            lo, hi = 0, 2
        elif any(w in lname for w in ("size", "limit", "count", "quantity", "amount")):
            lo, hi = 1, 20
        else:
            lo, hi = 0, 100
    elif lo is None:
        lo = min(0, hi) if hi >= 0 else hi - 100
    elif hi is None:
        hi = lo + 100
    if lo > hi:
        lo, hi = hi, lo
    multiple = schema.get("multipleOf")
    if integer:
        value = rng.randint(int(lo), int(hi))
        if multiple:
            value = int(value // multiple * multiple) or int(multiple)
        return value
    value = round(rng.uniform(lo, hi), 2)
    if multiple:
        value = round(value / multiple) * multiple
    return value


def _gen_string(schema: dict, mode: Mode, name: str) -> str:
    lo = min(schema.get("minLength", 0), MAX_STRING)
    hi = schema.get("maxLength")
    if hi is not None:
        hi = min(hi, MAX_STRING)
    if mode == Mode.BOUNDARY and schema.get("format") is None and schema.get("pattern") is None:
        n = rng.choice([lo, hi if hi is not None else 255])
        return "".join(rng.choice(string.ascii_letters) for _ in range(n))

    pattern = schema.get("pattern")
    candidates = [_by_format(schema.get("format"))] if schema.get("format") else []
    candidates += [_by_name(name), _random_word()]
    if pattern:
        candidates += _pattern_candidates(pattern, lo, hi)
        fits = lambda c: lo <= len(c) <= (hi if hi is not None else MAX_STRING)
        candidates = ([c for c in candidates if c is not None and _matches(pattern, c) and fits(c)]
                      or [c for c in candidates if c is not None and _matches(pattern, c)]
                      or candidates)

    value = next((c for c in candidates if c is not None), _random_word())
    if len(value) < lo:
        value = value + "x" * (lo - len(value))
    if hi is not None and len(value) > hi:
        value = value[:hi]
    return value


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------
def _by_format(fmt: Optional[str]) -> Optional[str]:
    today = date.today() - timedelta(days=rng.randint(1, 3000))
    return {
        "date": today.isoformat(),
        "date-time": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "time": "12:30:00",
        "email": f"{rng.choice(FIRST_NAMES).lower()}{rng.randint(1, 999)}@example.com",
        "uuid": _uuid(),
        "uri": f"http://example.com/{rng.choice(WORDS)}",
        "url": f"http://example.com/{rng.choice(WORDS)}",
        "hostname": "example.com",
        "ipv4": f"192.168.{rng.randint(0, 255)}.{rng.randint(1, 254)}",
        "ipv6": "::1",
        "password": "Passw0rd!" + str(rng.randint(10, 99)),
        "byte": "aGVsbG8=",
        "binary": "hello",
        "int32": str(rng.randint(1, 100)),
        "int64": str(rng.randint(1, 100)),
    }.get(fmt or "")


def _by_name(name: str) -> Optional[str]:
    n = name.lower()
    if not n:
        return None
    if "email" in n or "mail" in n:
        return _by_format("email")
    if "firstname" in n or n in ("first", "given", "givenname"):
        return rng.choice(FIRST_NAMES)
    if "lastname" in n or n in ("last", "surname", "familyname"):
        return rng.choice(LAST_NAMES)
    if n in ("name", "fullname", "username", "login", "user"):
        return rng.choice(FIRST_NAMES) + ("" if n == "name" else str(rng.randint(1, 99)))
    if "card" in n or n.startswith("cc") or "pan" == n:
        return "4111111111111111"
    if "cvv" in n or "cvc" in n:
        return "123"
    if "phone" in n or "telephone" in n or "mobile" in n:
        return "".join(rng.choice(string.digits) for _ in range(10))
    if "city" in n:
        return rng.choice(CITIES)
    if "address" in n or "street" in n:
        return f"{rng.randint(1, 200)} Main Street"
    if "zip" in n or "postal" in n:
        return str(rng.randint(10000, 99999))
    if "country" in n:
        return rng.choice(["IT", "IE", "US", "IN", "FR"])
    if "currency" in n:
        return rng.choice(["EUR", "USD", "INR"])
    if "lang" in n or "locale" in n:
        return "en"
    if "date" in n or "birth" in n:
        return _by_format("date")
    if "time" in n:
        return _by_format("date-time")
    if "url" in n or "uri" in n or "link" in n:
        return _by_format("uri")
    if "uuid" in n or "guid" in n:
        return _uuid()
    if n.endswith("id") or n == "id":
        return str(rng.randint(1, 10))
    if "password" in n or "secret" in n:
        return _by_format("password")
    if "code" in n:
        return rng.choice(string.ascii_uppercase) * 3
    if "description" in n or "comment" in n or "text" in n or "content" in n or "message" in n:
        return " ".join(rng.choice(WORDS) for _ in range(4))
    return None


def _random_word() -> str:
    return rng.choice(WORDS) + str(rng.randint(1, 999))


def _uuid() -> str:
    import uuid
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _matches(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return True


def from_regex(pattern: str) -> Optional[str]:
    """Build a random string matching `pattern` by walking Python's parsed regex.
    Returns None for syntax Python can't parse (e.g. Java's \\p{L})."""
    try:
        from re import _parser as sre_parse  # type: ignore[attr-defined]
        from re import _constants as c  # type: ignore[attr-defined]
        tree = sre_parse.parse(pattern)
    except Exception:
        return None

    pools = {
        c.CATEGORY_DIGIT: string.digits, c.CATEGORY_NOT_DIGIT: string.ascii_letters,
        c.CATEGORY_WORD: string.ascii_letters + string.digits + "_", c.CATEGORY_NOT_WORD: "-.!",
        c.CATEGORY_SPACE: " ", c.CATEGORY_NOT_SPACE: string.ascii_letters + string.digits,
    }

    def pick_in(items) -> str:
        allowed: list[str] = []
        negate = False
        for kind, val in items:
            if kind is c.NEGATE:
                negate = True
            elif kind is c.LITERAL:
                allowed.append(chr(val))
            elif kind is c.RANGE:
                allowed += [chr(x) for x in range(val[0], min(val[1], val[0] + 200) + 1)]
            elif kind is c.CATEGORY:
                allowed += list(pools.get(val, string.ascii_letters))
        if negate:
            options = [ch for ch in string.ascii_letters + string.digits if ch not in allowed]
            return rng.choice(options or ["x"])
        return rng.choice(allowed or ["x"])

    def walk(nodes, depth=0) -> str:
        out = []
        for kind, val in nodes:
            if depth > 20:
                break
            if kind is c.LITERAL:
                out.append(chr(val))
            elif kind is c.NOT_LITERAL:
                out.append("a" if chr(val) != "a" else "b")
            elif kind is c.ANY:
                out.append(rng.choice(string.ascii_letters))
            elif kind is c.IN:
                out.append(pick_in(val))
            elif kind in (c.MAX_REPEAT, c.MIN_REPEAT, getattr(c, "POSSESSIVE_REPEAT", None)):
                lo, hi, sub = val
                hi = lo + 3 if hi is c.MAXREPEAT else hi
                count = lo if rng.random() < 0.6 else rng.randint(lo, min(hi, lo + 3))
                out += [walk(sub, depth + 1) for _ in range(count)]
            elif kind is c.SUBPATTERN:
                out.append(walk(val[-1], depth + 1))
            elif kind is c.BRANCH:
                out.append(walk(rng.choice(val[1]), depth + 1))
            elif kind is c.CATEGORY:
                out.append(rng.choice(pools.get(val, string.ascii_letters)))
            # AT (^, $, \\b), GROUPREF, ASSERT...: emit nothing
        return "".join(out)

    try:
        value = walk(tree.data if hasattr(tree, "data") else tree)
    except Exception:
        return None
    return value if _matches(pattern, value) else None


def _pattern_candidates(pattern: str, lo: int, hi: Optional[int]) -> list[str]:
    """Cheap guesses that satisfy most patterns seen in real specs (digits only, letters only...).
    Lengths come from minLength/maxLength and from {m,n} quantifiers inside the pattern."""
    lengths = {max(lo, 1, min(hi or 6, 6))}
    for m, n in re.findall(r"\{(\d+)(?:,(\d*))?\}", pattern):
        lengths.add(int(m))
        if n:
            lengths.add(int(n))
    out = [x for x in (from_regex(pattern) for _ in range(3)) if x is not None]
    for n in sorted(x for x in lengths if 0 < x <= 64):
        out += [
            "".join(rng.choice(string.digits) for _ in range(n)),
            "".join(rng.choice(string.ascii_lowercase) for _ in range(n)),
            "".join(rng.choice(string.ascii_uppercase) for _ in range(n)),
            rng.choice(string.ascii_uppercase) + "".join(rng.choice(string.ascii_lowercase) for _ in range(n)),
            "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n)),
        ]
    return out + [_by_format("date"), _by_format("email"), _uuid()]


# ---------------------------------------------------------------------------
# INVALID mode
# ---------------------------------------------------------------------------
def _param_bag(req: Request, location: str) -> Optional[dict]:
    return {"path": req.path_params, "query": req.query, "header": req.headers,
            "cookie": req.cookies, "formData": req.form}.get(location)


def _lookup(pool, name: str, schema: dict, op) -> Any:
    if pool is None:
        return None
    try:
        return pool.lookup(name, schema, op)
    except Exception:
        return None


def _lookup_object(pool, resource: str) -> Any:
    fn = getattr(pool, "lookup_object", None)
    if fn is None:
        return None
    try:
        obj = fn(resource)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _break_one_location(req: Request, op: Operation) -> None:
    """Break exactly one thing: a parameter, a body field, or the whole body."""
    rb = op.request_body
    body = req.form if req.form else req.body
    choices: list[str] = [f"param:{p.name}" for p in op.parameters]
    if rb is not None:
        choices += ["body"]
        if isinstance(body, dict) and body:
            choices += ["field"] * 3  # body fields are where most bugs hide
    if not choices:
        return
    pick = rng.choice(choices)

    if pick.startswith("param:"):
        p = next(x for x in op.parameters if x.name == pick[6:])
        target = _param_bag(req, p.location)
        if target is None:
            return
        if p.required and p.location != "path" and rng.random() < 0.2:
            target.pop(p.name, None)  # drop a required param
            return
        bad = invalid_value(p.schema, p.name)
        target[p.name] = bad if p.location in ("path", "query") else serialize_param(bad)
    elif pick == "field":
        _break_body_field(body, rb.schema if rb else {})
    elif rng.random() < 0.3:
        req.raw_body = rng.choice([b"{malformed json", b"", b"\x00\xff", b"[1,2", b"null"])
    else:
        req.form = {}
        req.body = rng.choice([None, [], 12345, "", invalid_value(rb.schema if rb else {})])


def _break_body_field(body: dict, schema: dict) -> None:
    """Pick a field (possibly nested) and replace it with an invalid value or delete it."""
    props = schema.get("properties", {})
    keys = [k for k in body if k in props] or list(body)
    if not keys:
        return
    key = rng.choice(keys)
    sub = props.get(key, {})
    if isinstance(body[key], dict) and rng.random() < 0.5:
        _break_body_field(body[key], sub)
        return
    if key in schema.get("required", []) and rng.random() < 0.25:
        del body[key]
    else:
        body[key] = invalid_value(sub, key)
