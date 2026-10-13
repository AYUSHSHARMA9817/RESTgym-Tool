"""5XX fault signatures, novelty tracking and the bandit used to pick what to try next.

Signature = (status, operation, normalised error message). The message is taken
from the usual JSON error fields (message/error/detail/exception/trace) or the
raw body, and normalised so that two crashes that differ only in ids, numbers,
timestamps, uuids, quoted user input or memory addresses collapse together.
That mirrors how REST League counts "sufficiently distinct error messages".
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Hashable, Optional

from ..models import Operation, Request, Response

MSG_KEYS = ("message", "error", "detail", "details", "exception", "title", "error_description",
            "errorMessage", "cause", "reason", "description")
TRACE_KEYS = ("trace", "stackTrace", "stacktrace", "stack")

_SUBS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<date>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"@[0-9a-f]{4,}\b", re.I), "@<addr>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']{0,200}'"), "'<s>'"),
    (re.compile(r'"[^"]{0,200}"'), '"<s>"'),
    (re.compile(r"(?<![a-z_])[-+]?\d+(\.\d+)?(e[-+]?\d+)?", re.I), "<n>"),
    (re.compile(r"\s+"), " "),
]
# lines of an HTML error page that carry no information
_HTML_TAG = re.compile(r"<[^>]+>")


def _extract_message(resp: Response) -> str:
    j = resp.json
    parts: list[str] = []
    if isinstance(j, dict):
        for k in MSG_KEYS:
            v = j.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
            elif isinstance(v, dict):  # {"error": {"message": ...}}
                for kk in MSG_KEYS:
                    if isinstance(v.get(kk), str):
                        parts.append(v[kk])
        for k in TRACE_KEYS:
            v = j.get(k)
            if isinstance(v, str) and v:
                # first frames identify the crash site better than the message
                parts.append(" | ".join(v.strip().splitlines()[:3]))
            elif isinstance(v, list) and v:
                parts.append(" | ".join(str(x) for x in v[:3]))
        if not parts:
            parts.append(json.dumps({k: v for k, v in j.items() if k not in ("timestamp", "time", "path", "requestId")},
                                    sort_keys=True, default=str)[:400])
    elif isinstance(j, str):
        parts.append(j)
    elif resp.text:
        t = resp.text
        if "<html" in t[:500].lower() or "<!doctype" in t[:200].lower():
            m = re.search(r"<title>(.*?)</title>", t, re.I | re.S)
            body = _HTML_TAG.sub(" ", t)
            parts.append((m.group(1) if m else "") + " " + body[:300])
        else:
            parts.append(t[:400])
    return " || ".join(parts)


def normalise(msg: str) -> str:
    m = msg[:1000]
    for rx, rep in _SUBS:
        m = rx.sub(rep, m)
    return m.strip().lower()[:300]


def signature(op_key: str, resp: Response, mode: str = "op_message") -> tuple:
    """Dedup key for a 5XX. ``mode`` controls how coarse the dedup is:

    - "op_message" (default): (status, operation, normalised message) — a crash
      is distinct if it hits a different endpoint OR carries a different message.
    - "message": (status, normalised message) only — API-wide distinct messages,
      matching tools that dedup purely on the error text.
    - "op_status": (status, operation) — one fault per crashing endpoint.
    """
    msg = normalise(_extract_message(resp))
    if mode == "message":
        return (resp.status, msg)
    if mode == "op_status":
        return (resp.status, op_key)
    return (resp.status, op_key, msg)


@dataclass
class Fault:
    sig: tuple
    first_seen: float
    status: int = 500
    op: str = ""
    msg: str = ""
    count: int = 1
    example_mutation: Optional[str] = None
    example_request: dict = field(default_factory=dict)
    example_response: str = ""


class FaultTracker:
    """Keeps unique 5XX signatures. ``record`` returns True when the fault is new."""

    def __init__(self, log_path: Optional[str] = None, dedup_mode: Optional[str] = None):
        self.faults: dict[tuple, Fault] = {}
        self.by_op: dict[str, int] = defaultdict(int)          # unique faults per op
        self.hits_by_op: dict[str, int] = defaultdict(int)     # total 5XX per op
        self.messages: set[str] = set()                        # normalised messages (API-wide)
        self.timeline: list[tuple[float, int]] = []
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._log_path = log_path or os.environ.get("FAULT_LOG")
        # how to count a "unique" fault; align with the competition's Restats rule
        self.dedup_mode = dedup_mode or os.environ.get("FAULT_DEDUP", "op_message")

    def record(self, op_key: str, req: Request, resp: Response) -> bool:
        if not resp.is_5xx:
            return False
        sig = signature(op_key, resp, self.dedup_mode)
        msg = normalise(_extract_message(resp))
        with self._lock:
            self.hits_by_op[op_key] += 1
            if sig in self.faults:
                self.faults[sig].count += 1
                return False
            f = Fault(sig=sig, first_seen=time.monotonic() - self._t0, status=resp.status,
                      op=op_key, msg=msg, example_mutation=req.mutation,
                      example_request=_req_dict(req), example_response=resp.text[:2000])
            self.faults[sig] = f
            self.by_op[op_key] += 1
            self.messages.add(msg)
            self.timeline.append((f.first_seen, len(self.faults)))
        self._log(f)
        return True

    def _log(self, f: Fault) -> None:
        if not self._log_path:
            return
        try:
            with open(self._log_path, "a") as fh:
                fh.write(json.dumps({"t": round(f.first_seen, 2), "status": f.status, "op": f.op,
                                     "msg": f.msg, "mutation": f.example_mutation,
                                     "request": f.example_request, "response": f.example_response[:500]},
                                    default=str) + "\n")
        except OSError:
            pass

    @property
    def unique(self) -> int:
        return len(self.faults)

    def summary(self) -> dict:
        with self._lock:
            return {
                "unique_5xx": len(self.faults),
                "unique_messages": len(self.messages),
                "dedup_mode": self.dedup_mode,
                "by_op": dict(self.by_op),
                "faults": [{"status": f.status, "op": f.op, "msg": f.msg, "count": f.count,
                            "t": round(f.first_seen, 1), "mutation": f.example_mutation}
                           for f in self.faults.values()],
            }


def _req_dict(req: Request) -> dict:
    d = {"method": req.method, "path": req.path, "path_params": req.path_params, "query": req.query,
         "headers": req.headers, "content_type": req.content_type}
    if req.raw_body is not None:
        d["raw_body"] = req.raw_body[:500].decode("utf-8", "replace")
    else:
        d["body"] = req.body if req.body is not None else req.form
    return d


# ------------------------------------------------------------------ bandit
class Bandit:
    """Discounted UCB1 over arbitrary hashable arms.

    Rewards decay with ``gamma`` per pull so the bandit keeps adapting when an
    arm stops producing new faults (5XX novelty dries up quickly).
    """

    def __init__(self, arms: list[Hashable] | None = None, c: float = 0.6, gamma: float = 0.995,
                 prior: float = 0.5, seed: Optional[int] = None):
        self.c, self.gamma, self.prior = c, gamma, prior
        self.n: dict[Hashable, float] = {}
        self.r: dict[Hashable, float] = {}
        self.total = 0.0
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        for a in arms or []:
            self.add(a)

    def add(self, arm: Hashable) -> None:
        with self._lock:
            self.n.setdefault(arm, 0.0)
            self.r.setdefault(arm, 0.0)

    def remove(self, arm: Hashable) -> None:
        with self._lock:
            self.n.pop(arm, None)
            self.r.pop(arm, None)

    def score(self, arm: Hashable) -> float:
        n = self.n.get(arm, 0.0)
        mean = (self.r[arm] + self.prior) / (n + 1.0)
        return mean + self.c * math.sqrt(math.log(self.total + 2.0) / (n + 1.0))

    def select(self, weights: Optional[dict[Hashable, float]] = None) -> Hashable:
        with self._lock:
            arms = list(self.n)
            if not arms:
                raise ValueError("bandit has no arms")
            best, best_s = None, -1e18
            for a in arms:
                s = self.score(a) * (weights.get(a, 1.0) if weights else 1.0)
                s += self._rng.random() * 1e-6
                if s > best_s:
                    best, best_s = a, s
            return best

    def update(self, arm: Hashable, reward: float) -> None:
        with self._lock:
            if arm not in self.n:
                return
            for a in self.n:
                self.n[a] *= self.gamma
                self.r[a] *= self.gamma
            self.total = sum(self.n.values())
            self.n[arm] += 1.0
            self.r[arm] += reward
            self.total += 1.0
