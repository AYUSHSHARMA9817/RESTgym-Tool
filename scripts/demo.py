"""A clean, recordable demo run (and a preview of the `specseed scan` CLI).

Unlike the campaign runner, this does NOT restart or tear down the target: it
brings the API up once, runs Track A against the live instance, prints the
confirmed violations with a copy-pasteable curl, and leaves the stack running so
the reproduction command works on camera.

    python -m scripts.demo                 # crapi, 90s
    BUDGET=45 python -m scripts.demo       # shorter, for a tight clip
    python -m scripts.demo vampi           # a different wired-up target

Stop the target afterwards with:
    python -c "from eval.apis import stop; stop('crapi')"
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.apis import (APIS, alt_auth_for, auth_for, fuzz_exclude,  # noqa: E402
                       public_authz_paths, start)
from restleague.engine.client import ApiClient                       # noqa: E402
from restleague.spec.deps import order_operations                    # noqa: E402
from restleague.spec.parser import parse_spec                        # noqa: E402
from restleague.track_a import TrackARunner                          # noqa: E402

API = sys.argv[1] if len(sys.argv) > 1 else "crapi"
BUDGET = float(os.environ.get("BUDGET", "90"))
SEED = int(os.environ.get("SEED", "7"))

_AUTHZ = {"cross_user_access", "missing_auth", "broken_function_auth", "cross_entity_access"}


def _public_fp(f) -> bool:
    if f.oracle not in _AUTHZ:
        return False
    path = f.op_key.split(" ", 1)[-1]
    return any(path.startswith(p) for p in public_authz_paths(API))


async def main() -> int:
    if API not in APIS:
        print(f"unknown API {API!r}; known: {', '.join(APIS)}")
        return 2

    print(f"\n  SpecSeed — finding broken-authorization bugs in {API}, black-box.\n")
    print("  bringing the target up (leaves it running for reproduction)...")
    base = start(API)
    print(f"  up at {base}\n  fuzzing for {BUDGET:.0f}s with 3 identities "
          f"(owner · another user · no credentials)...\n")

    ops = order_operations(parse_spec(f"specs/{API}-openapi.json"))
    excl = fuzz_exclude(API)
    fuzz_ops = [o for o in ops if (o.method, o.path) not in excl] if excl else ops

    client = ApiClient(base, timeout=10.0, auth=auth_for(API))
    unauth = ApiClient(base, timeout=10.0) if auth_for(API) else None
    alt_prov = alt_auth_for(API)
    alt = ApiClient(base, timeout=10.0, auth=alt_prov) if alt_prov else None

    runner = TrackARunner(fuzz_ops, base, budget_seconds=BUDGET, seed=SEED,
                          client=client, api=API, unauth_client=unauth, alt_client=alt)
    res = await runner.run_async()
    for c in (client, unauth, alt):
        if c is not None:
            await c.close()

    findings = [f for f in runner.findings.findings if not _public_fp(f)]
    violations = [f for f in findings if f.severity != "warning"]
    warnings = [f for f in findings if f.severity == "warning"]

    print("  " + res.describe())
    print(f"\n  == CONFIRMED VIOLATIONS: {len(violations)} "
          f"(+ {len(warnings)} warnings for review) ==\n")
    for i, f in enumerate(violations, 1):
        print(f"  {i}. [{f.oracle}] {f.op_key}")
        print(f"     {f.summary}")
        for line in f.as_curl(base)[:4]:
            print(f"       {line}")
        print()

    print(f"  target still running at {base} — run a curl above to reproduce live.")
    print(f"  when done:  python -c \"from eval.apis import stop; stop('{API}')\"\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
