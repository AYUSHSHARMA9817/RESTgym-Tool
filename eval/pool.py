"""A small resource pool for the evaluation runner. It implements the interface that
The engine offers to the generator: lookup(name, schema, op) and lookup_object(resource).
It remembers ids and objects seen in 2XX responses, keyed by deps.resource_key()."""
import random
from typing import Any, Optional

from restleague.models import Operation, Response
from restleague.spec.deps import keys_match, param_key, produced_keys, resource_key


class SimplePool:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.ids: dict[str, list[Any]] = {}
        self.objects: dict[str, list[dict]] = {}

    # -- harvesting --------------------------------------------------------
    def record(self, op: Operation, resp: Response) -> None:
        if not resp.ok or resp.json is None:
            return
        segments = [s for s in op.path.strip("/").split("/") if s and not s.startswith("{")]
        collection = resource_key(segments[-1]) if segments else ""
        main_key = next(iter(produced_keys(op)), collection)
        self._walk(resp.json, main_key)

    def _walk(self, node: Any, key: str) -> None:
        if isinstance(node, list):
            for x in node[:50]:
                self._walk(x, key)
        elif isinstance(node, dict):
            if "id" in node and isinstance(node["id"], (int, str)) and key:
                self._add(self.ids, key, node["id"])
                self._add(self.objects, key, node)
            for k, v in node.items():
                if k != "id" and k.lower().endswith("id") and isinstance(v, (int, str)):
                    self._add(self.ids, resource_key(k), v)
                elif isinstance(v, (dict, list)):
                    self._walk(v, resource_key(k))

    @staticmethod
    def _add(store: dict, key: str, value: Any) -> None:
        bucket = store.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)
            del bucket[:-100]  # keep the newest 100

    # -- the interface the generator uses --------------------------------
    def _find(self, store: dict, key: str) -> Optional[list]:
        if store.get(key):
            return store[key]
        matches = [k for k in store if store[k] and keys_match(k, key)]
        return store[min(matches, key=len)] if matches else None

    def lookup(self, name: str, schema: dict, op: Operation) -> Any:
        is_path = any(p.name == name and p.location == "path" for p in op.parameters)
        key = param_key(op, name) if is_path else resource_key(name)
        found = self._find(self.ids, key)
        return self.rng.choice(found) if found else None

    def lookup_object(self, resource: str) -> Optional[dict]:
        found = self._find(self.objects, resource_key(resource))
        return dict(self.rng.choice(found)) if found else None
