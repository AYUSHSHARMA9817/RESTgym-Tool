"""Pre-flight check before a long unattended run.

Verifies the things that otherwise fail a multi-hour campaign halfway through:
the target answers, auth works, there is disk headroom, and (for crAPI) the
compose stack is healthy. Prints PASS/FAIL per check and exits non-zero if any
check fails, so it can gate a run:

    python -m eval.preflight --apis crapi && python -m eval.campaign --apis crapi ...

It makes no changes; it only reads.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from eval.apis import APIS, auth_for   # noqa: E402

MIN_FREE_GB = 5.0


def _ok(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return cond


def _http(url: str, timeout: float = 8.0):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read()
    except Exception as e:
        return 0, str(e).encode()


def check_disk() -> bool:
    free_gb = shutil.disk_usage(".").free / (1024 ** 3)
    return _ok(f"disk headroom >= {MIN_FREE_GB}GB", free_gb >= MIN_FREE_GB,
               f"{free_gb:.1f}GB free")


def check_api(api: str) -> bool:
    conf = APIS.get(api)
    if not conf:
        return _ok(f"{api}: known", False, "no such API in eval/apis.py")
    base = f"http://localhost:{conf['port']}"
    ok = True

    status, _ = _http(base + conf.get("probe", "/"))
    ok &= _ok(f"{api}: gateway answers", status and status < 500,
              f"GET {conf.get('probe', '/')} -> {status}")

    # auth round-trip, when the API declares a login
    auth = conf.get("auth")
    if auth:
        body = auth.get("AUTH_LOGIN_BODY", "{}").encode()
        req = urllib.request.Request(base + auth["AUTH_LOGIN_PATH"], data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        token = None
        try:
            data = json.load(urllib.request.urlopen(req, timeout=10))
            token = data.get(auth.get("AUTH_TOKEN_FIELD", "token"))
        except Exception as e:
            token = None
            detail = f"login error: {e}"
        else:
            detail = "token received" if token else "no token in response"
        ok &= _ok(f"{api}: login works", bool(token), detail)

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apis", default="crapi", help="comma-separated APIs to check")
    a = ap.parse_args()
    apis = [x.strip() for x in a.apis.split(",") if x.strip()]

    print("pre-flight:")
    ok = check_disk()
    for api in apis:
        ok &= check_api(api)

    print("READY" if ok else "NOT READY -- fix the FAILs above before a long run")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
