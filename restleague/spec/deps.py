"""Producer/consumer analysis + operation ordering.

Resources are identified by a normalized key such as "ownerid":
  POST /owners                      produces "ownerid"
  GET  /owners/{ownerId}            consumes "ownerid"
  POST /owners/{ownerId}/pets       consumes "ownerid", produces "petid"
  GET  /pets/{id}                   consumes "petid"   ({id} named after the segment before it)
"""
import re

from restleague.models import Operation

METHOD_RANK = {"POST": 0, "PUT": 2, "PATCH": 2, "GET": 1, "HEAD": 3, "OPTIONS": 3, "DELETE": 4}


def dependencies(ops: list[Operation]) -> dict[str, dict[str, set[str]]]:
    """{op.key: {"produces": {...}, "consumes": {...}}} - resource keys like "ownerid"."""
    return {op.key: {"produces": produced_keys(op), "consumes": consumed_keys(op)} for op in ops}


def order_operations(ops: list[Operation]) -> list[Operation]:
    """Sort: producers before consumers, collection-level creates first, DELETEs last."""
    depth = _dependency_depths(ops)

    def sort_key(op: Operation):
        is_delete = op.method == "DELETE"
        is_create = op.method == "POST" and not op.path.rstrip("/").endswith("}")
        # deletes run children first (deepest first) so parents are still there
        level = -depth[op.key] if is_delete else depth[op.key]
        return (is_delete, level, not is_create, METHOD_RANK.get(op.method, 3),
                op.path.count("/"), op.path)

    return sorted(ops, key=sort_key)


def resource_key(name: str) -> str:
    """'owner_id' / 'ownerId' / 'OwnerID' -> 'ownerid'; 'owners' -> 'ownerid'."""
    n = re.sub(r"[^a-z0-9]", "", name.lower())
    if n.endswith("id"):
        return n
    return _singular(n) + "id"


def param_key(op: Operation, param_name: str) -> str:
    """The resource key a path parameter refers to. A bare {id} takes the name of the
    path segment before it: /owners/{id} -> 'ownerid'."""
    if re.sub(r"[^a-z0-9]", "", param_name.lower()) in ("id", "key", "uuid", "code", "name"):
        segments = op.path.strip("/").split("/")
        for i, seg in enumerate(segments):
            if seg == "{" + param_name + "}" and i > 0 and not segments[i - 1].startswith("{"):
                return resource_key(segments[i - 1])
    return resource_key(param_name)


def consumed_keys(op: Operation) -> set[str]:
    keys = {param_key(op, p.name) for p in op.parameters if p.location == "path"}
    # required body fields that reference other resources, e.g. {"ownerId": 1} or {"type": {"id": 1}}
    body = op.request_body.schema if op.request_body else {}
    for prop, sub in body.get("properties", {}).items():
        if sub.get("readOnly") or prop not in body.get("required", []):
            continue
        if prop.lower().endswith("id") and prop.lower() != "id":
            keys.add(resource_key(prop))
        elif "id" in (sub.get("properties") or {}):
            keys.add(resource_key(prop))
    return keys


def produced_keys(op: Operation) -> set[str]:
    """A POST to a collection (path not ending in a parameter) creates one of that collection."""
    if op.method not in ("POST", "PUT"):
        return set()
    segments = [s for s in op.path.strip("/").split("/") if s]
    if not segments or segments[-1].startswith("{"):
        return set()
    if op.method == "PUT" and not _returns_object_with_id(op):
        return set()
    return {resource_key(segments[-1])}


def _returns_object_with_id(op: Operation) -> bool:
    for status, schema in op.responses.items():
        if status.startswith("2") and "id" in (schema or {}).get("properties", {}):
            return True
    return False


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _dependency_depths(ops: list[Operation]) -> dict[str, int]:
    """depth(op) = 1 + max depth of the ops producing what it consumes (0 if none).
    Iterates to a fixed point; cycles are cut off by the iteration limit."""
    producers: dict[str, list[Operation]] = {}
    for op in ops:
        for key in produced_keys(op):
            producers.setdefault(key, []).append(op)
    consumes = {op.key: {_match_producer(k, producers) for k in consumed_keys(op)} for op in ops}

    depth = {op.key: 0 for op in ops}
    for _ in range(len(ops)):
        changed = False
        for op in ops:
            needed = [depth[p.key] for key in consumes[op.key] for p in producers.get(key, []) if p is not op]
            new = 1 + max(needed) if needed else 0
            if new > depth[op.key] and new <= len(ops):
                depth[op.key] = new
                changed = True
        if not changed:
            break
    return depth


def keys_match(a: str, b: str) -> bool:
    """Do two resource keys name the same resource?
    'typeid' ~ 'pettypeid' (one ends with the other);
    'hospitaiid' ~ 'hospitalid' (long common prefix: plurals the English rule
    in _singular() gets wrong, such as Portuguese 'hospitais')."""
    if a == b:
        return True
    if min(len(a), len(b)) > 2 and (a.endswith(b) or b.endswith(a)):  # a bare "id" matches nothing
        return True
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= 6


def _match_producer(key: str, producers: dict) -> str:
    """The producer key for a consumed key: exact match first, else the shortest match."""
    if key in producers:
        return key
    matches = [k for k in producers if keys_match(k, key)]
    return min(matches, key=len) if matches else key
