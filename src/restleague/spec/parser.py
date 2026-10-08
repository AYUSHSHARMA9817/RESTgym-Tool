"""Owner: A. OpenAPI 3 spec file -> list[Operation] with every $ref resolved."""
import json
from typing import Any, Optional
from urllib.parse import urlparse

from restleague.models import Operation, Parameter, RequestBody

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
# Per the OpenAPI spec, header parameters with these names are ignored.
IGNORED_HEADERS = {"accept", "content-type", "authorization"}


def parse_spec(path: str) -> list[Operation]:
    spec = _load(path)
    base = _base_path(spec)

    global_security = spec.get("security", [])
    ops = []
    for p, item in spec.get("paths", {}).items():
        item = _resolve(item, spec)
        shared = item.get("parameters", [])
        for method, raw in item.items():
            if method not in HTTP_METHODS:
                continue
            ops.append(Operation(
                method=method.upper(),
                path=base + p,
                operation_id=raw.get("operationId", ""),
                parameters=_merge_params(shared, raw.get("parameters", [])),
                request_body=_request_body(raw.get("requestBody")),
                responses={status: _pick_content(r.get("content"))[1] or {}
                           for status, r in raw.get("responses", {}).items()},
                tags=raw.get("tags", []),
                produces=_response_media_types(raw.get("responses", {})),
                security=raw.get("security", global_security),
            ))
    return ops


def _load(path: str) -> Any:
    with open(path) as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import yaml  # only needed for YAML specs
        return yaml.safe_load(text)


def _base_path(spec: dict) -> str:
    """'http://h:8080/api/v2/' -> '/api/v2'. Host and port come from env vars, not the spec."""
    servers = spec.get("servers") or [{}]
    return urlparse(servers[0].get("url", "")).path.rstrip("/")


def _follow_pointer(spec: dict, ref: str) -> Any:
    """'#/components/schemas/Pet' -> spec['components']['schemas']['Pet']."""
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _resolve(node: Any, spec: dict, seen: frozenset = frozenset()) -> Any:
    """Return a copy of node with every $ref expanded, allOf merged and oneOf/anyOf
    reduced to their first option. `seen` holds the refs we are currently inside of;
    meeting one of them again is a cycle and becomes a plain object."""
    if isinstance(node, list):
        return [_resolve(x, spec, seen) for x in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {"type": "object"}
        return _resolve(_follow_pointer(spec, ref), spec, seen | {ref})

    node: dict[str, Any] = {k: _resolve(v, spec, seen) for k, v in node.items()}
    if "allOf" in node:
        node = _merge_all_of(node)
    for key in ("oneOf", "anyOf"):
        if node.get(key):
            return node[key][0]
    return node


def _merge_all_of(node: dict) -> dict:
    merged = {k: v for k, v in node.items() if k != "allOf"}
    properties, required = {}, []
    for part in node["allOf"]:
        properties.update(part.get("properties", {}))
        required += [r for r in part.get("required", []) if r not in required]
    merged["type"] = "object"
    merged["properties"] = properties
    if required:
        merged["required"] = required
    return merged


def _merge_params(shared: list, own: list) -> list[Parameter]:
    """Path-level params first; an operation param with the same (name, in) replaces it."""
    by_key = {}
    for q in shared + own:
        if q.get("in") == "header" and q["name"].lower() in IGNORED_HEADERS:
            continue
        by_key[(q["name"], q["in"])] = Parameter(
            name=q["name"],
            location=q["in"],
            required=q["in"] == "path" or q.get("required", False),
            schema=q.get("schema", {}),
            style=q.get("style"),
            explode=q.get("explode"),
        )
    return list(by_key.values())


def _pick_content(content: Any) -> tuple[Any, Any]:
    """Prefer application/json, else the first media type. Returns (type, schema)."""
    if not content:
        return None, None
    media = "application/json" if "application/json" in content else next(iter(content))
    return media, content[media].get("schema", {})


def _request_body(raw: Any) -> Optional[RequestBody]:
    """Preferred media type first in content_types; its schema in .schema."""
    if not raw or not raw.get("content"):
        return None
    content = raw["content"]
    media, schema = _pick_content(content)
    return RequestBody(
        schema=schema,
        content_types=[media] + [m for m in content if m != media],
        required=raw.get("required", False),
        schemas={m: c.get("schema", {}) for m, c in content.items()},
    )


def _response_media_types(responses: dict) -> list[str]:
    types: list[str] = []
    for r in responses.values():
        types += [m for m in (r.get("content") or {}) if m not in types]
    return types
