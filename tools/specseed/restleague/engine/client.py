"""HTTP client: send a Request, get a Response. Never raises.

Auth configuration (environment variables, all optional):
    AUTH_HEADER        "Authorization: Bearer xyz"  (several separated by "\\n" or ";;")
    AUTH_QUERY         "api_key=xyz"                (several separated by "&")
    AUTH_COOKIE        "session=abc"                (several separated by ";")
    AUTH_LOGIN_PATH    "/api/login"  -> enables LoginTokenProvider
    AUTH_LOGIN_METHOD  default POST
    AUTH_LOGIN_BODY    JSON body, e.g. '{"username":"admin","password":"admin"}'
    AUTH_TOKEN_FIELD   dotted path into the login JSON, default: first of token/access_token/jwt/id_token
    AUTH_TOKEN_HEADER  default "Authorization"
    AUTH_TOKEN_PREFIX  default "Bearer "
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx

from ..models import Request, Response

log = logging.getLogger("restleague.client")

# httpx logs every request at INFO; far too noisy for an hour of fuzzing
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

DEFAULT_TIMEOUT = 10.0


# ------------------------------------------------------------------- auth
class AuthProvider(Protocol):
    async def apply(self, req_headers: dict, req_query: dict, req_cookies: dict, client: "ApiClient") -> None: ...
    async def on_unauthorized(self, client: "ApiClient") -> bool:
        """Called after a 401. Return True if credentials were refreshed (request will be retried once)."""
        ...


class StaticAuth:
    """Headers / query / cookies taken verbatim from env vars."""

    def __init__(self, headers: dict[str, str] | None = None, query: dict[str, str] | None = None,
                 cookies: dict[str, str] | None = None):
        self.headers = headers or {}
        self.query = query or {}
        self.cookies = cookies or {}

    @classmethod
    def from_env(cls, env=os.environ) -> "StaticAuth":
        headers, query, cookies = {}, {}, {}
        raw = env.get("AUTH_HEADER", "")
        for part in raw.replace(";;", "\n").splitlines():
            if ":" in part:
                k, v = part.split(":", 1)
                if k.strip():
                    headers[k.strip()] = v.strip()
        for part in env.get("AUTH_QUERY", "").split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k.strip()] = v.strip()
        for part in env.get("AUTH_COOKIE", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cls(headers, query, cookies)

    def __bool__(self) -> bool:
        return bool(self.headers or self.query or self.cookies)

    async def apply(self, h, q, c, client) -> None:
        for k, v in self.headers.items():
            h.setdefault(k, v)
        for k, v in self.query.items():
            q.setdefault(k, v)
        for k, v in self.cookies.items():
            c.setdefault(k, v)

    async def on_unauthorized(self, client) -> bool:
        return False


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


class LoginTokenProvider:
    """Fetches a token from a login endpoint and injects it as a header. Refreshes on 401."""

    TOKEN_KEYS = ("token", "access_token", "accessToken", "jwt", "id_token", "idToken", "auth_token")

    def __init__(self, path: str, body: Any = None, method: str = "POST", token_field: str | None = None,
                 header: str = "Authorization", prefix: str = "Bearer "):
        self.path, self.body, self.method = path, body, method.upper()
        self.token_field, self.header, self.prefix = token_field, header, prefix
        self.token: Optional[str] = None
        self._lock = asyncio.Lock()
        self._last_fetch = 0.0

    @classmethod
    def from_env(cls, env=os.environ) -> Optional["LoginTokenProvider"]:
        path = env.get("AUTH_LOGIN_PATH")
        if not path:
            return None
        body = env.get("AUTH_LOGIN_BODY")
        try:
            body = json.loads(body) if body else None
        except ValueError:
            pass
        return cls(path, body, env.get("AUTH_LOGIN_METHOD", "POST"), env.get("AUTH_TOKEN_FIELD"),
                   env.get("AUTH_TOKEN_HEADER", "Authorization"), env.get("AUTH_TOKEN_PREFIX", "Bearer "))

    def _extract(self, resp: Response) -> Optional[str]:
        if self.token_field and resp.json is not None:
            v = _dig(resp.json, self.token_field)
            return str(v) if v else None
        if isinstance(resp.json, dict):
            stack = [resp.json]
            while stack:
                d = stack.pop()
                for k in self.TOKEN_KEYS:
                    if isinstance(d.get(k), str):
                        return d[k]
                stack.extend(v for v in d.values() if isinstance(v, dict))
        for hk in ("authorization", "x-auth-token", "x-access-token"):
            if hk in resp.headers:
                return resp.headers[hk].removeprefix("Bearer ").strip()
        if isinstance(resp.json, str):
            return resp.json
        return None

    async def fetch(self, client: "ApiClient") -> bool:
        async with self._lock:
            if time.monotonic() - self._last_fetch < 2:  # another worker just refreshed
                return self.token is not None
            self._last_fetch = time.monotonic()
            req = Request(method=self.method, path=self.path, body=self.body,
                          content_type="application/json" if self.body is not None else None)
            resp = await client.send(req, _auth=False)
            tok = self._extract(resp) if resp.ok else None
            if tok:
                self.token = tok
                log.info("auth: obtained token from %s", self.path)
                return True
            log.warning("auth: login at %s failed (status %s)", self.path, resp.status)
            return False

    async def apply(self, h, q, c, client) -> None:
        if self.token is None and self._last_fetch == 0.0:
            await self.fetch(client)
        if self.token:
            h.setdefault(self.header, f"{self.prefix}{self.token}")

    async def on_unauthorized(self, client) -> bool:
        return await self.fetch(client)


class CompositeAuth:
    def __init__(self, providers: list):
        self.providers = providers

    async def apply(self, h, q, c, client) -> None:
        for p in self.providers:
            await p.apply(h, q, c, client)

    async def on_unauthorized(self, client) -> bool:
        res = False
        for p in self.providers:
            res = (await p.on_unauthorized(client)) or res
        return res


def auth_from_env(env=os.environ) -> Optional[AuthProvider]:
    providers: list = []
    static = StaticAuth.from_env(env)
    if static:
        providers.append(static)
    login = LoginTokenProvider.from_env(env)
    if login:
        providers.append(login)
    if not providers:
        return None
    return providers[0] if len(providers) == 1 else CompositeAuth(providers)


# ------------------------------------------------------------------ client
def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return str(v)


def fill_path(template: str, params: dict[str, Any]) -> str:
    out = template
    for k, v in params.items():
        if isinstance(v, list):
            v = ",".join(_scalar(x) for x in v)
        out = out.replace("{" + k + "}", quote(_scalar(v), safe=""))
    return out


class ApiClient:
    """Async HTTP client with retry/backoff and liveness tracking.

    ``api_down`` becomes True after ``down_threshold`` consecutive transport
    failures; the strategy should pause and call ``wait_until_alive``.
    """

    def __init__(self, base_url: str, auth: Optional[AuthProvider] = None, timeout: float = DEFAULT_TIMEOUT,
                 max_connections: int = 16, retries: int = 2, down_threshold: int = 8):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.retries = retries
        self.down_threshold = down_threshold
        self.consecutive_failures = 0
        self.total_requests = 0
        self.total_transport_errors = 0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            follow_redirects=False,
            verify=False,
        )

    @property
    def api_down(self) -> bool:
        return self.consecutive_failures >= self.down_threshold

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover
            pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # -- building -------------------------------------------------------
    def _build(self, req: Request, headers: dict, query: dict, cookies: dict) -> dict:
        url = self.base_url + fill_path(req.path, req.path_params)
        params: list[tuple[str, str]] = []
        for k, v in query.items():
            if isinstance(v, list):
                params.extend((k, _scalar(x)) for x in v)
            else:  # None is sent as "k=" on purpose (useful for fuzzing)
                params.append((k, _scalar(v)))
        kw: dict[str, Any] = {"method": req.method.upper(), "url": url, "params": params}
        hdrs = {str(k): _scalar(v) for k, v in headers.items()}
        if cookies:
            cookie_hdr = "; ".join(f"{k}={_scalar(v)}" for k, v in cookies.items())
            hdrs["Cookie"] = (hdrs.get("Cookie", "") + "; " + cookie_hdr).lstrip("; ")
        ct = req.content_type
        if req.raw_body is not None:
            kw["content"] = req.raw_body
            if ct:
                hdrs.setdefault("Content-Type", ct)
        elif ct and "multipart/form-data" in ct:
            data, files = {}, {}
            for k, v in (req.form or (req.body if isinstance(req.body, dict) else {})).items():
                if isinstance(v, (bytes, bytearray)):
                    files[k] = (f"{k}.bin", bytes(v), "application/octet-stream")
                else:
                    data[k] = _scalar(v)
            if not files:  # httpx needs at least one file to produce multipart
                files["__rl"] = ("", b"", "application/octet-stream")
            kw["data"], kw["files"] = data, files
        elif ct and "x-www-form-urlencoded" in ct:
            src = req.form or (req.body if isinstance(req.body, dict) else {})
            kw["content"] = "&".join(f"{quote(str(k))}={quote(_scalar(v))}" for k, v in src.items()).encode()
            hdrs.setdefault("Content-Type", ct)
        elif req.body is not None or (ct and "json" in ct and req.method.upper() in ("POST", "PUT", "PATCH")):
            if ct and "json" not in ct and isinstance(req.body, (str, bytes)):
                kw["content"] = req.body.encode() if isinstance(req.body, str) else req.body
            else:
                kw["content"] = json.dumps(req.body, default=str).encode()
            hdrs.setdefault("Content-Type", ct or "application/json")
        elif req.form:
            kw["data"] = {k: _scalar(v) for k, v in req.form.items()}
        hdrs.setdefault("Accept", "application/json, */*;q=0.8")
        kw["headers"] = hdrs
        return kw

    # -- sending --------------------------------------------------------
    async def send(self, req: Request, _auth: bool = True) -> Response:
        headers, query, cookies = dict(req.headers), dict(req.query), dict(req.cookies)
        if _auth and self.auth is not None:
            try:
                await self.auth.apply(headers, query, cookies, self)
            except Exception as e:  # never let auth break the run
                log.debug("auth.apply failed: %s", e)
        try:
            kw = self._build(req, headers, query, cookies)
        except Exception as e:
            return Response(status=0, error=f"build error: {e!r}")

        resp = await self._send_with_retry(kw)
        if resp.status == 401 and _auth and self.auth is not None:
            try:
                if await self.auth.on_unauthorized(self):
                    headers, query, cookies = dict(req.headers), dict(req.query), dict(req.cookies)
                    await self.auth.apply(headers, query, cookies, self)
                    resp = await self._send_with_retry(self._build(req, headers, query, cookies))
            except Exception as e:
                log.debug("auth refresh failed: %s", e)
        return resp

    async def _send_with_retry(self, kw: dict) -> Response:
        delay = 0.5
        last_err = ""
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                self.total_requests += 1
                r = await self._client.request(**kw)
                elapsed = time.monotonic() - t0
                self.consecutive_failures = 0
                text = r.text
                parsed = None
                if text and ("json" in r.headers.get("content-type", "") or text[:1] in "[{\""):
                    try:
                        parsed = r.json()
                    except Exception:
                        parsed = None
                return Response(status=r.status_code, headers={k.lower(): v for k, v in r.headers.items()},
                                text=text, json=parsed, elapsed=elapsed)
            except httpx.TimeoutException as e:
                # A timeout usually means the server is slow on *this* input; don't hammer it again.
                self.total_transport_errors += 1
                return Response(status=0, error=f"timeout: {type(e).__name__}", elapsed=time.monotonic() - t0)
            except (httpx.TransportError, OSError) as e:
                self.total_transport_errors += 1
                self.consecutive_failures += 1
                last_err = f"{type(e).__name__}: {e}"
                if attempt < self.retries:
                    await asyncio.sleep(delay)
                    delay *= 2
            except Exception as e:  # httpx.InvalidURL, encoding problems, ...
                return Response(status=0, error=f"{type(e).__name__}: {e}")
        return Response(status=0, error=last_err)

    async def probe(self) -> bool:
        """Cheap liveness check: any HTTP answer counts as alive."""
        try:
            await self._client.request("GET", self.base_url + "/", timeout=3.0)
            self.consecutive_failures = 0
            return True
        except Exception:
            return False

    async def wait_until_alive(self, deadline: float, max_sleep: float = 10.0) -> bool:
        delay = 0.5
        while time.monotonic() < deadline:
            if await self.probe():
                log.warning("API is reachable again")
                return True
            await asyncio.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, max_sleep)
        return False
