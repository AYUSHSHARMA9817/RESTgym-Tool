"""Optional LLM prerequisite planner -- HTTP-only, cached, deterministic.

**Off by default.** Without ``LLM_PLANNER`` set, ``make_planner`` returns a
``NullPlanner`` and Track A runs exactly as it always has: spec-only, no AI, no
new dependency (the ``anthropic`` package is never imported). This is the
"without AI" arm of the evaluation -- the baseline.

**With ``LLM_PLANNER=1``** the planner supplies concrete *value hints* for
request fields whose valid value a schema-only generator cannot guess (an
enumeration named in a property description, a status string, a format the spec
states only in prose). The hints change which values are *sent* to reach a deep
state; they never touch the oracles that decide whether a response is a bug.

The design in one line: **LLM for reachability, sound oracles for detection.**
A wrong hint, at worst, fails to reach a state -- it can never manufacture a
false finding. That separation is what preserves the no-false-positive
guarantee even with a nondeterministic model in the loop.

**Determinism.** Every solved hint set is cached to a JSON file keyed by a
stable signature of the operation (method, path, and a hash of its request
schema). The model is consulted once per novel operation; a committed cache then
replays the whole run offline, so results reproduce without an API key. That is
what lets the "with AI" numbers be checked by a reviewer who has no model
access.

The planner lives entirely in Track A (``restleague/sequence``); the frozen
competition engine never sees it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional, Protocol, runtime_checkable

from ..models import Operation

log = logging.getLogger("restleague.planner")

DEFAULT_MODEL = "claude-sonnet-5-5"   # a bulk extractor; cheap, bounded calls


def op_signature(op: Operation) -> str:
    """Stable cache key for an operation: method, path, and a hash of the
    request schema, so a changed contract re-plans rather than reusing a stale
    hint."""
    schema = op.request_body.schema if op.request_body else None
    blob = json.dumps(schema, sort_keys=True, default=str) if schema else ""
    digest = hashlib.sha1(blob.encode()).hexdigest()[:8]
    return f"{op.method.upper()} {op.path}#{digest}"


@runtime_checkable
class Planner(Protocol):
    """Returns ``{field: value}`` hints to apply to an operation's request body,
    or an empty dict for none."""

    def hints(self, op: Operation) -> dict[str, Any]: ...


class NullPlanner:
    """The default, AI-free planner: never suggests anything, so request
    generation is exactly the spec-only baseline."""

    def hints(self, op: Operation) -> dict[str, Any]:
        return {}


@runtime_checkable
class Backend(Protocol):
    """Proposes value hints for one operation. May be slow / nondeterministic;
    the CachingPlanner calls it at most once per operation signature."""

    def propose(self, op: Operation) -> dict[str, Any]: ...


class CachingPlanner:
    """Wraps a backend with a persistent JSON cache.

    On a hit, no backend call is made -- this is what makes a run with a
    committed cache fully deterministic and runnable offline. A miss calls the
    backend once and stores the result (even an empty one, so a barren operation
    is not re-queried every window).
    """

    def __init__(self, backend: Backend, cache_path: Optional[str] = None):
        self.backend = backend
        self.cache_path = cache_path
        self._cache: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.cache_path and os.path.exists(self.cache_path):
            try:
                with open(self.cache_path) as fh:
                    self._cache = json.load(fh)
            except Exception as e:
                log.warning("planner cache unreadable (%s); starting empty", e)
                self._cache = {}

    def _persist(self) -> None:
        if not (self.cache_path and self._dirty):
            return
        try:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._cache, fh, indent=2, sort_keys=True, default=str)
            os.replace(tmp, self.cache_path)
            self._dirty = False
        except Exception as e:
            log.debug("could not persist planner cache: %s", e)

    def hints(self, op: Operation) -> dict[str, Any]:
        if op.request_body is None:
            return {}                       # nothing to hint without a body
        sig = op_signature(op)
        if sig in self._cache:
            return dict(self._cache[sig])
        try:
            proposed = self.backend.propose(op) or {}
        except Exception as e:
            log.debug("planner backend failed for %s: %s", op.key, e)
            proposed = {}
        if not isinstance(proposed, dict):
            proposed = {}
        self._cache[sig] = proposed
        self._dirty = True
        self._persist()
        if proposed:
            log.info("planner hints for %s: %s", op.key, list(proposed))
        return dict(proposed)


_SYSTEM = (
    "You help a black-box API fuzzer satisfy an endpoint's input constraints so "
    "it can reach deeper application states. You are given one OpenAPI operation. "
    "Return a JSON object mapping request-body field names to concrete valid "
    "values, ONLY for fields whose correct value a schema-only generator would "
    "miss: enumerations or allowed values named in a description, required status "
    "strings, specific formats, or values that must plausibly match real data. "
    "Use only fields that the request schema declares. Do not invent fields. If "
    "the schema already fully determines valid values, return {}. "
    "Output a single JSON object and nothing else."
)


class LLMBackend:
    """Anthropic-backed hint proposer. Imported lazily so the package is only a
    dependency when the planner is actually switched on."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self._client = None

    def _client_or_none(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except Exception as e:
                log.warning("LLM planner unavailable (%s); running spec-only", e)
                self._client = False          # sentinel: tried and failed
        return self._client or None

    def propose(self, op: Operation) -> dict[str, Any]:
        client = self._client_or_none()
        if client is None:
            return {}
        payload = {
            "method": op.method,
            "path": op.path,
            "operation_id": op.operation_id,
            "parameters": [{"name": p.name, "in": p.location, "schema": p.schema}
                           for p in (op.parameters or [])],
            "request_schema": op.request_body.schema if op.request_body else None,
        }
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            )
        except Exception as e:
            log.debug("LLM planner call failed for %s: %s", op.key, e)
            return {}
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse_json_object(text)


def _parse_json_object(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of the model's reply; {} on any trouble."""
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def enabled(env=os.environ) -> bool:
    """Is the LLM planner switched on? Default: no (tool runs without AI)."""
    return str(env.get("LLM_PLANNER", "")).strip().lower() in ("1", "true", "yes", "on")


def make_planner(env=os.environ, api: str = "") -> Planner:
    """Build the planner for this run.

    Without ``LLM_PLANNER`` set -> ``NullPlanner`` (no AI, no dependency, exact
    spec-only baseline). With it set -> a ``CachingPlanner`` over the Anthropic
    backend, writing/reading a per-API cache so runs stay reproducible.
    """
    if not enabled(env):
        return NullPlanner()
    cache = env.get("LLM_PLANNER_CACHE") or (
        os.path.join("specs", f"{api}.planner-cache.json") if api else None)
    model = env.get("LLM_PLANNER_MODEL", DEFAULT_MODEL)
    return CachingPlanner(LLMBackend(model), cache_path=cache)
