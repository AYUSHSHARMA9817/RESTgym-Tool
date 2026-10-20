"""ViolationTracker: unique-ifies oracle violations the way FaultTracker does 5XXs.

Milestone 3/5 of docs/architecture/track_a_stateful_fuzzing.md. The engine's
FaultTracker counts distinct *crashes*; this counts distinct *semantic* bugs,
and is kept separate so engine/faults.py -- which the competition entry point
depends on -- does not have to change.

A violation's signature is (oracle, operation, normalised summary). The summary
is normalised with the same substitutions FaultTracker uses, so two reports that
differ only in an id or a timestamp collapse into one finding.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from typing import Iterable, Optional

from ..engine.faults import normalise
from ..models import OracleViolation


class ViolationTracker:
    """Keeps unique oracle violations. ``record`` returns True when one is new."""

    def __init__(self, log_path: Optional[str] = None):
        self.violations: dict[tuple, OracleViolation] = {}
        self.counts: dict[tuple, int] = defaultdict(int)
        self.by_oracle: dict[str, int] = defaultdict(int)
        self.by_op: dict[str, int] = defaultdict(int)
        self.timeline: list[tuple[float, int]] = []
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._log_path = log_path or os.environ.get("VIOLATION_LOG")

    @staticmethod
    def signature(v: OracleViolation) -> tuple:
        return (v.oracle, v.op_key, normalise(v.summary))

    def record(self, v: OracleViolation) -> bool:
        sig = self.signature(v)
        with self._lock:
            self.counts[sig] += 1
            if sig in self.violations:
                return False
            self.violations[sig] = v
            self.by_oracle[v.oracle] += 1
            self.by_op[v.op_key] += 1
            self.timeline.append((time.monotonic() - self._t0, len(self.violations)))
        self._log(v)
        return True

    def record_all(self, violations: Iterable[OracleViolation]) -> int:
        """Record many; returns how many were new."""
        return sum(1 for v in violations if self.record(v))

    def _log(self, v: OracleViolation) -> None:
        if not self._log_path:
            return
        try:
            with open(self._log_path, "a") as fh:
                fh.write(json.dumps({"t": round(time.monotonic() - self._t0, 2),
                                     "oracle": v.oracle, "op": v.op_key, "summary": v.summary,
                                     "severity": v.severity, "evidence": v.evidence},
                                    default=str) + "\n")
        except OSError:
            pass

    @property
    def unique(self) -> int:
        return len(self.violations)

    def summary(self) -> dict:
        with self._lock:
            return {
                "unique_violations": len(self.violations),
                "by_oracle": dict(self.by_oracle),
                "by_op": dict(self.by_op),
                "violations": [
                    {"oracle": v.oracle, "op": v.op_key, "summary": v.summary,
                     "severity": v.severity, "count": self.counts[sig],
                     "evidence": v.evidence}
                    for sig, v in self.violations.items()
                ],
            }
