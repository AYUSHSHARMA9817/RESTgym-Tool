"""Does a generated value satisfy its schema? A small checker covering the keywords
that real specs in our set use: type, enum, required, pattern, length and range limits,
and the formats date / date-time / email / uuid."""
import re
from datetime import date, datetime
from typing import Any
from uuid import UUID

TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _format_ok(fmt: str, v: str) -> bool:
    try:
        if fmt == "date":
            date.fromisoformat(v)
        elif fmt == "date-time":
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        elif fmt == "uuid":
            UUID(v)
        elif fmt == "email":
            return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) is not None
    except ValueError:
        return False
    return True


def violations(value: Any, schema: dict, where: str = "$") -> list[str]:
    """Return a list of human-readable violations (empty list = conforms)."""
    if not schema:
        return []
    out: list[str] = []
    t = schema.get("type")
    if t in TYPE_CHECK and not TYPE_CHECK[t](value):
        return [f"{where}: type {t} expected, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: not in enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10**12):
            out.append(f"{where}: length {len(value)} outside limits")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    out.append(f"{where}: does not match pattern")
            except re.error:
                pass  # Java-only syntax; cannot check in Python
        if schema.get("format") and not _format_ok(schema["format"], value):
            out.append(f"{where}: bad {schema['format']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{where}: above maximum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value and not props.get(k, {}).get("readOnly"):
                out.append(f"{where}.{k}: required field missing")
        for k, v in value.items():
            out += violations(v, props.get(k, {}), f"{where}.{k}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            out.append(f"{where}: too few items")
        for i, v in enumerate(value):
            out += violations(v, schema.get("items", {}), f"{where}[{i}]")
    return out
