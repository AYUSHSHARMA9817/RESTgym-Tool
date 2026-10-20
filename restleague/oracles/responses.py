"""Response-inspection oracles: reflected payloads, timing injection, CORS.

All three are passive -- they read the request/response pairs the run already
produced and send nothing of their own. The injection payloads the attack step
already carries are what they key on; the point of Tier 3 is to *look at* the
responses we were already provoking instead of only watching for a 5XX.

* **ReflectedPayloadOracle (B5)** -- an XSS-ish payload that comes back verbatim
  in the response body. In an HTML/XML response that is reflected XSS (a
  violation); in any other body it is a warning, since a correctly JSON-encoded
  echo is data, not script. Verbatim matching is the soundness: a server that
  escaped the payload (``&lt;script&gt;`` or ``\\u003c``) will not match.

* **TimingInjectionOracle (B4)** -- a request carrying a time-delay payload
  (``SLEEP``, ``pg_sleep``, ``WAITFOR``, ``benchmark``) that then takes about as
  long as the delay asks for. This is how blind, error-free injection shows
  itself: the only signal is the clock.

* **CorsOracle (E6)** -- ``Access-Control-Allow-Origin: *`` together with
  ``Access-Control-Allow-Credentials: true``. The combination is forbidden by
  the CORS spec and lets any site make credentialed cross-origin calls.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

from ..models import OracleViolation, Request, Sequence
from .base import Oracle, OracleContext

# markers that make a string dangerous in an HTML context
_XSS_MARKERS = ("<script", "onerror=", "onload=", "<svg", "<img", "javascript:",
                "<iframe", "</script>")
# time-delay primitives across SQL dialects / NoSQL
_SLEEP_MARKERS = ("sleep(", "pg_sleep", "waitfor delay", "benchmark(", "sleep ")
HTML_TYPES = ("text/html", "application/xhtml", "application/xml", "text/xml")
SLOW_SECONDS = 2.5          # a sleep payload asks for ~3s; flag at 2.5s


def _string_values(req: Request) -> Iterator[str]:
    """Every string the request put on the wire -- body, query, path, form."""
    def walk(v: Any) -> Iterator[str]:
        if isinstance(v, str):
            yield v
        elif isinstance(v, dict):
            for x in v.values():
                yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)
    for bag in (req.body, req.query, req.path_params, req.form):
        yield from walk(bag)


class ReflectedPayloadOracle(Oracle):
    """A dangerous payload echoed back verbatim -- reflected XSS in HTML."""
    name = "reflected_payload"
    active = False
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or not resp.text:
                continue
            body = resp.text
            ctype = (resp.headers or {}).get("content-type", "").lower()
            is_html = any(t in ctype for t in HTML_TYPES)
            for val in _string_values(req):
                low = val.lower()
                if len(val) < 4 or not any(m in low for m in _XSS_MARKERS):
                    continue
                if val not in body:
                    continue              # escaped or not echoed -> safe
                if is_html:
                    out.append(self.violation(
                        req.op_key,
                        f"an HTML response reflects a script payload verbatim: {val[:50]!r}",
                        severity="violation", content_type=ctype, payload=val[:120]))
                else:
                    out.append(self.violation(
                        req.op_key,
                        f"the response echoes a script payload verbatim ({ctype or 'no type'}); "
                        f"exploitable if ever rendered as HTML: {val[:50]!r}",
                        severity="warning", content_type=ctype, payload=val[:120]))
                break                     # one reflection per step is enough
        return out


class TimingInjectionOracle(Oracle):
    """A time-delay payload that actually delayed the response -- blind injection."""
    name = "timing_injection"
    active = False
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            req, resp = step.request, step.response
            if resp is None or resp.elapsed < SLOW_SECONDS:
                continue
            payload = next((v for v in _string_values(req)
                            if any(m in v.lower() for m in _SLEEP_MARKERS)), None)
            if payload is None:
                continue
            out.append(self.violation(
                req.op_key,
                f"a request carrying a time-delay payload took {resp.elapsed:.1f}s "
                f"-- time-based blind injection: {payload[:50]!r}",
                severity="violation", elapsed=round(resp.elapsed, 2), payload=payload[:120]))
        return out


class CorsOracle(Oracle):
    """Wildcard origin together with credentials is a forbidden, exploitable combo."""
    name = "cors_misconfig"
    active = False
    mutating = False

    async def check(self, seq: Sequence, ctx: OracleContext) -> list[OracleViolation]:
        out: list[OracleViolation] = []
        for step in seq.steps:
            resp = step.response
            if resp is None or not resp.headers:
                continue
            h = resp.headers
            origin = h.get("access-control-allow-origin", "")
            creds = h.get("access-control-allow-credentials", "").lower()
            if origin == "*" and creds == "true":
                out.append(self.violation(
                    step.request.op_key,
                    "Access-Control-Allow-Origin: * with Allow-Credentials: true "
                    "-- any site can make credentialed cross-origin requests",
                    severity="violation", allow_origin=origin))
        return out
