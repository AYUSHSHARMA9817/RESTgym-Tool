"""Send a models.Request with the standard library only."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from restleague.models import Request, Response
from restleague.spec.values import serialize_param


def send(base_url: str, req: Request, timeout: float = 10.0) -> Response:
    path = req.path
    for name, value in req.path_params.items():
        path = path.replace("{" + name + "}", urllib.parse.quote(serialize_param(value), safe=""))
    query = []
    for name, value in req.query.items():
        for v in (value if isinstance(value, list) else [value]):
            query.append((name, serialize_param(v)))
    url = base_url.rstrip("/") + path + ("?" + urllib.parse.urlencode(query) if query else "")

    headers = {k: str(v) for k, v in req.headers.items()}
    data = None
    if req.raw_body is not None:
        data = req.raw_body
    elif req.form:
        data = urllib.parse.urlencode({k: serialize_param(v) for k, v in req.form.items()}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif req.body is not None:
        data = json.dumps(req.body, default=str).encode() if not isinstance(req.body, str) else req.body.encode()
    if data is not None and "Content-Type" not in headers:
        headers["Content-Type"] = req.content_type or "application/json"
    headers.setdefault("Accept", "application/json")

    start = time.perf_counter()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, method=req.method, headers=headers),
                                   timeout=timeout)
        status, text, rh = r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        status, text, rh = e.code, e.read().decode("utf-8", "replace"), dict(e.headers or {})
    except Exception as e:  # connection refused, timeout, bad URL...
        return Response(status=0, error=str(e), elapsed=time.perf_counter() - start)
    try:
        body = json.loads(text) if text else None
    except ValueError:
        body = None
    return Response(status=status, headers=rh, text=text, json=body, elapsed=time.perf_counter() - start)
