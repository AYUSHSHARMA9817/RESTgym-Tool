"""ResourcePool: ids and objects harvested from successful responses.

the spec component's value generator calls ``pool.lookup(param_name, schema, op)`` and uses the
result when it is not None. Matching is name based (normalised: lower-case, no
``_``/``-``) with resource-aware fallbacks:

    POST /owners -> {"id": 7, "name": "x"}
        stored as  id=7, ownerid=7, owner.id=7, name="x", ownername="x"
    GET /owners/{ownerId}   -> lookup("ownerId")  -> 7
    GET /pets?owner_id=...  -> lookup("owner_id") -> 7
"""
from __future__ import annotations

import random
import re
import threading
from collections import defaultdict, deque
from typing import Any, Iterable, Optional

from ..models import Operation, Request, Response

MAX_PER_KEY = 64
MAX_OBJECTS = 32
_PARAM_RE = re.compile(r"\{([^}]+)\}")


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def resource_of(path: str) -> Optional[str]:
    """Last literal (non-parameter) segment, singularised. '/api/owners/{id}/pets' -> 'pet'."""
    segs = [s for s in path.strip("/").split("/") if s and not _PARAM_RE.fullmatch(s)]
    segs = [s for s in segs if not re.fullmatch(r"v\d+|api|rest", s.lower())]
    return norm(singular(segs[-1])) if segs else None


def resource_for_param(path: str, param: str) -> Optional[str]:
    """Resource a path parameter refers to: segment right before '{param}'."""
    segs = path.strip("/").split("/")
    for i, s in enumerate(segs):
        if s == "{" + param + "}" and i > 0 and not _PARAM_RE.fullmatch(segs[i - 1]):
            return norm(singular(segs[i - 1]))
    return None


def _type_ok(value: Any, schema: Optional[dict]) -> bool:
    if not schema:
        return not isinstance(value, (dict, list))
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t in (None, "string"):
        return isinstance(value, (str, int)) and not isinstance(value, bool)
    if t == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, str) and value.lstrip("-").isdigit())
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    return True


def _coerce(value: Any, schema: Optional[dict]) -> Any:
    if not schema:
        return value
    t = schema.get("type")
    if t == "integer" and isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if t == "string" and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value


class ResourcePool:
    def __init__(self, seed: Optional[int] = None):
        self._values: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_PER_KEY))
        self._objects: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_OBJECTS))
        self._dead: set = set()           # values known to be deleted
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ storing
    def add(self, key: str, value: Any) -> None:
        if value is None or isinstance(value, (dict, list)):
            return
        if isinstance(value, str) and (len(value) > 256 or not value):
            return
        k = norm(key)
        if not k:
            return
        with self._lock:
            dq = self._values[k]
            if value in dq:
                dq.remove(value)
            dq.append(value)           # most recent at the right
            self._dead.discard(_hashable(value))

    def add_object(self, resource: str, obj: dict) -> None:
        if resource and isinstance(obj, dict) and obj:
            with self._lock:
                self._objects[norm(resource)].append(obj)

    def forget(self, value: Any) -> None:
        """Mark a value as deleted (after a successful DELETE)."""
        with self._lock:
            self._dead.add(_hashable(value))

    # ---------------------------------------------------------- harvesting
    def harvest(self, op: Operation, req: Request, resp: Response) -> None:
        """Call after every response. Only 2XX responses are harvested."""
        if not resp.ok:
            return
        res = resource_of(op.path)
        method = op.method.upper()

        if method == "DELETE":
            for v in req.path_params.values():
                self.forget(v)
            return

        # path params that just worked are valid ids
        for name, v in req.path_params.items():
            self.add(name, v)
            r = resource_for_param(op.path, name)
            if r:
                self.add(r + "id", v)

        if resp.json is not None:
            self._walk(resp.json, res, depth=0)
            if isinstance(resp.json, dict) and res:
                self.add_object(res, resp.json)
            elif isinstance(resp.json, list) and res:
                for item in resp.json[:5]:
                    if isinstance(item, dict):
                        self.add_object(res, item)
        elif resp.text and method == "POST" and len(resp.text) < 64 and resp.text.strip().strip('"').isalnum():
            # some APIs return the bare id as text
            v = resp.text.strip().strip('"')
            self.add("id", int(v) if v.isdigit() else v)
            if res:
                self.add(res + "id", int(v) if v.isdigit() else v)

        loc = resp.headers.get("location")
        if loc:
            last = loc.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if last:
                val: Any = int(last) if last.isdigit() else last
                self.add("id", val)
                if res:
                    self.add(res + "id", val)

        # body values that the API accepted on create/update are realistic examples
        if method in ("POST", "PUT", "PATCH") and isinstance(req.body, dict):
            for k, v in req.body.items():
                if not isinstance(v, (dict, list)):
                    self.add(f"{res or ''}.{k}" if res else k, v)
            if res:
                self.add_object(res + ".request", req.body)

    def _walk(self, node: Any, res: Optional[str], depth: int, parent: Optional[str] = None) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            # an object nested under key "owner" belongs to resource "owner"
            here = norm(singular(parent)) if parent else res
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    self._walk(v, res, depth + 1, parent=k)
                    if isinstance(v, dict) and v:
                        self.add_object(norm(singular(k)), v)
                    continue
                self.add(k, v)
                if here:
                    nk = norm(k)
                    if not nk.startswith(here):
                        self.add(here + nk, v)       # id -> ownerid, name -> ownername
                    self.add(f"{here}.{k}", v)
        elif isinstance(node, list):
            for item in node[:20]:
                self._walk(item, res, depth + 1, parent=parent)

    # -------------------------------------------------------------- lookup
    def _candidates(self, keys: Iterable[str], schema: Optional[dict]) -> list:
        out = []
        with self._lock:
            for k in keys:
                for v in self._values.get(k, ()):
                    if _hashable(v) not in self._dead and _type_ok(v, schema):
                        out.append(v)
                if out:
                    break
        return out

    def lookup(self, param_name: str, schema: Optional[dict] = None, op: Optional[Operation] = None,
               fresh_bias: float = 0.7) -> Any:
        """Return a harvested value suitable for ``param_name`` or None.

        ``fresh_bias``: probability of returning the most recently seen value
        (fresh resources are the most likely to still exist).
        """
        n = norm(param_name)
        keys: list[str] = [n]
        res = resource_for_param(op.path, param_name) if op else None
        own = resource_of(op.path) if op else None
        if n == "id" and res:
            keys.insert(0, res + "id")
        if n.endswith("id") and n != "id":
            stem = n[:-2]
            keys += [stem + "id", stem + ".id", singular(stem) + "id"]
        if res:
            keys += [res + "id", f"{res}.{param_name.lower()}"]
        if n == "id" and own:
            keys += [own + "id"]
        if n.endswith(("id", "ids", "key", "code", "name", "uuid")) or n in ("id",):
            keys.append("id" if n.endswith(("id", "ids")) else n)
        # de-duplicate, keep order
        seen, ordered = set(), []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                ordered.append(k)
        cands = self._candidates(ordered, schema)
        if not cands:
            return None
        v = cands[-1] if self._rng.random() < fresh_bias else self._rng.choice(cands)
        return _coerce(v, schema)

    def lookup_object(self, resource: str) -> Optional[dict]:
        with self._lock:
            objs = self._objects.get(norm(resource)) or self._objects.get(norm(singular(resource)))
            return dict(self._rng.choice(objs)) if objs else None

    def any_value(self, schema: Optional[dict] = None) -> Any:
        """Random harvested scalar of the right type (used by mutations)."""
        with self._lock:
            keys = list(self._values.keys())
        self._rng.shuffle(keys)
        for k in keys[:30]:
            c = self._candidates([k], schema)
            if c:
                return self._rng.choice(c)
        return None

    def dead_value(self) -> Any:
        """A value that used to be valid but was DELETEd (stale-reference fuzzing)."""
        with self._lock:
            return self._rng.choice(list(self._dead)) if self._dead else None

    def size(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._values.values())

    def has(self, key: str) -> bool:
        with self._lock:
            return bool(self._values.get(norm(key)))

    def snapshot(self, limit: int = 5) -> dict:
        with self._lock:
            return {k: list(v)[-limit:] for k, v in self._values.items()}


def _hashable(v: Any) -> Any:
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)
