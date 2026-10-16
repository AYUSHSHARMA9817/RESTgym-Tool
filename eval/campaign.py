"""Unattended bug-hunting campaign across real APIs.

Start it and leave it. Each round restarts an API from a clean container, runs
the tool against it, verifies every new finding on another clean container, and
appends what survives. Nothing is held in memory between rounds, so the results
are intact whatever happens to the process -- a failed container start, a crash
inside a run, or a Ctrl-C hours later.

    python3 -m eval.campaign --hours 8
    python3 -m eval.campaign --apis gestao-hospital --budget 600 --rounds 50
    python3 -m eval.campaign --jobs 3 --hours 12        # three APIs at once

APIs are hunted concurrently, one worker each, so wall-clock throughput scales
with how many you can run at once. Each API has its own containers and ports
(see eval/apis.py), so the workers never collide; --jobs caps how many run
together when memory is tight. Workers share only the findings store, which is
locked, and each keeps its own round counter and seeds.

Writes into --out (default: runs/):

    findings.jsonl   every finding, as it is found
    verified.jsonl   only those that reproduced on a clean container
    REPORT.md        regenerated after every round; safe to read mid-run
    campaign.log     what happened, including failures

Rounds use different seeds, so repeated rounds explore differently rather than
repeating themselves. A finding already confirmed in an earlier round is not
re-verified, since verification costs a container restart; new ones are.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.apis import APIS, alt_auth_for, auth_for, start, stop                          # noqa: E402
from restleague.engine.client import ApiClient                   # noqa: E402
from restleague.engine.strategy import Strategy                   # noqa: E402
from restleague.findings import verify                           # noqa: E402
from restleague.oracles import ALL_ORACLES                       # noqa: E402
from restleague.oracles.tracker import ViolationTracker          # noqa: E402
from restleague.spec.deps import order_operations                # noqa: E402
from restleague.spec.parser import parse_spec                    # noqa: E402
from restleague.track_a import TrackARunner                      # noqa: E402

STOP = False


def _stop(signum, frame):
    global STOP
    STOP = True
    print("\n[campaign] stopping after this round; results are already on disk", flush=True)


class Campaign:
    def __init__(self, apis: list[str], budget: float, out: str, engine_too: bool = True,
                 jobs: int = 0):
        self.apis = apis
        # How many APIs run at once. Each needs its own containers (a JVM, and
        # for gestao-hospital a MongoDB too), so this is bounded by memory
        # rather than CPU -- the work is almost entirely waiting on HTTP.
        self.jobs = jobs or len(apis)
        self._gate = asyncio.Semaphore(max(1, self.jobs))
        self._lock_report = asyncio.Lock()
        self.per_api: dict = {}
        self.budget = budget
        self.out = out
        self.engine_too = engine_too
        os.makedirs(out, exist_ok=True)
        self.findings_path = os.path.join(out, "findings.jsonl")
        self.verified_path = os.path.join(out, "verified.jsonl")
        self.log_path = os.path.join(out, "campaign.log")
        self.report_path = os.path.join(out, "REPORT.md")
        self.seen: set = set()          # violation signatures already verified
        self.verified: list = []
        self.crashes: dict = {}         # api -> {signature: example}
        self.rounds = 0
        self.failures = 0
        self.started = time.time()
        self._resume()

    # ------------------------------------------------------------- plumbing
    def log(self, msg: str) -> None:
        line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
        print(f"[campaign] {msg}", flush=True)
        try:
            with open(self.log_path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def _resume(self) -> None:
        """Pick up whatever earlier runs left, so a restart does not redo work."""
        if not os.path.exists(self.verified_path):
            return
        with open(self.verified_path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                self.verified.append(d)
                self.seen.add((d["oracle"], d["op_key"], d["summary"]))
        if self.verified:
            self.log(f"resuming with {len(self.verified)} finding(s) already confirmed")

    def _append(self, path: str, obj: dict) -> None:
        try:
            with open(path, "a") as fh:
                fh.write(json.dumps(obj, default=str) + "\n")
        except OSError:
            pass

    # --------------------------------------------------------------- a round
    async def round_for(self, api: str, seed: int) -> None:
        ops = order_operations(parse_spec(f"specs/{api}-openapi.json"))
        by_key = {o.key: o for o in ops}

        base = start(api)
        client = ApiClient(base, timeout=10.0, auth=auth_for(api))
        # Extra clients for the authorization oracles: one with no credentials,
        # one authenticated as a second user. Only built when the API has auth.
        unauth = ApiClient(base, timeout=10.0) if auth_for(api) else None
        alt_prov = alt_auth_for(api)
        alt = ApiClient(base, timeout=10.0, auth=alt_prov) if alt_prov else None
        runner = TrackARunner(ops, base, budget_seconds=self.budget, seed=seed,
                              client=client, findings_file=self.findings_path, api=api,
                              unauth_client=unauth, alt_client=alt)
        res = await runner.run_async()
        await client.close()
        for c in (unauth, alt):
            if c is not None:
                await c.close()
        self.log(f"{api} seed={seed}: {res.describe()}")
        self._note_crashes(api, res.faults)

        fresh = [f for f in runner.findings.findings
                 if (f.oracle, f.op_key, f.summary) not in self.seen]
        self.log(f"{api} seed={seed}: {len(fresh)} new finding(s) to verify")

        for f in fresh:
            key = (f.oracle, f.op_key, f.summary)
            self.seen.add(key)
            if STOP:
                break
            try:
                stop(api)
                vbase = start(api)                 # a clean container per finding
                vclient = ApiClient(vbase, timeout=10.0, auth=auth_for(api))
                # the authorization oracles replay through extra clients, so the
                # verify step needs them too or those findings never reproduce
                vunauth = ApiClient(vbase, timeout=10.0) if auth_for(api) else None
                valt_prov = alt_auth_for(api)
                valt = ApiClient(vbase, timeout=10.0, auth=valt_prov) if valt_prov else None
                ok = await verify(f, by_key, vclient,
                                  {cls.name: cls() for cls in ALL_ORACLES},
                                  unauth_client=vunauth, alt_client=valt)
                await vclient.close()
                for c in (vunauth, valt):
                    if c is not None:
                        await c.close()
            except Exception as e:
                self.log(f"  verify failed for {f.oracle}/{f.op_key}: {e!r}")
                continue
            if ok:
                rec = f.to_json()
                rec["curl"] = f.as_curl(f"http://HOST:{APIS[api]['port']}")
                self.verified.append(rec)
                self._append(self.verified_path, rec)
                self.log(f"  CONFIRMED [{f.oracle}] {f.op_key} :: {f.summary[:70]}")

        if self.engine_too and not STOP:
            stop(api)
            ebase = start(api)
            # Strategy directly rather than strategy.run(): that wrapper calls
            # asyncio.run(), which cannot nest inside this already-running loop.
            eclient = ApiClient(ebase, timeout=10.0, auth=auth_for(api))
            r = await Strategy(ops, ebase, budget_seconds=self.budget, seed=seed,
                               client=eclient).run_async()
            await eclient.close()
            self.log(f"{api} seed={seed}: engine {r.unique_5xx} unique 5XX, "
                     f"{r.operations_covered}/{len(ops)} covered")
            self._note_crashes(api, r.faults)

    def _note_crashes(self, api: str, faults: dict) -> None:
        bucket = self.crashes.setdefault(api, {})
        for f in faults.get("faults", []):
            bucket.setdefault((f.get("op"), f.get("msg")), f)

    # --------------------------------------------------------------- report
    def write_report(self) -> None:
        hrs = (time.time() - self.started) / 3600.0
        lines = [
            "# Bug-hunting campaign",
            "",
            f"_Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"{self.rounds} round(s) over {hrs:.1f}h · "
            f"{len(self.apis)} API(s) run {self.jobs} at a time_",
            "",
            "Every finding below reproduced on a container restarted from scratch, "
            "using only the requests listed with it. Reproducing shows a finding is "
            "real and deterministic; whether the behaviour is *wrong* is a judgement "
            "about the API's intent, so each still deserves a human read.",
            "",
            "## Semantic findings (verified)",
            "",
        ]
        if not self.verified:
            lines += ["_None confirmed yet._", ""]
        else:
            by_oracle = Counter(v["oracle"] for v in self.verified)
            lines += ["| count | oracle |", "|---|---|"]
            lines += [f"| {n} | `{o}` |" for o, n in by_oracle.most_common()]
            lines += [""]
            for i, v in enumerate(self.verified, 1):
                lines += [f"### {i}. {v['summary']}", "",
                          f"- **oracle**: `{v['oracle']}`",
                          f"- **operation**: `{v['op_key']}`",
                          f"- **API**: {v.get('api', '?')}", "",
                          "Reproduce from a clean instance:", "", "```bash"]
                lines += v.get("curl", [])[:8]
                lines += ["```", ""]

        lines += ["## Crashes (unique 5XX)", ""]
        total = sum(len(v) for v in self.crashes.values())
        if not total:
            lines += ["_None yet._", ""]
        for api, bucket in self.crashes.items():
            lines += [f"**{api}** — {len(bucket)} unique", ""]
            for (op, msg) in list(bucket)[:40]:
                lines += [f"- `{op}` — {str(msg)[:110]}"]
            lines += [""]

        lines += ["## Run health", "",
                  f"- rounds completed: {self.rounds}",
                  f"- rounds that errored: {self.failures}",
                  f"- findings confirmed: {len(self.verified)}",
                  f"- raw findings seen: {len(self.seen)}", ""]
        try:
            with open(self.report_path, "w") as fh:
                fh.write("\n".join(lines))
        except OSError:
            pass

    # ------------------------------------------------------------- the loop
    async def _worker(self, api: str, rounds: int, deadline) -> None:
        """Hunt one API, round after round, independently of the others."""
        seed = 1
        done = 0
        while not STOP:
            if rounds and done >= rounds:
                break
            if deadline and time.time() >= deadline:
                break
            async with self._gate:          # bounded by --jobs
                try:
                    await self.round_for(api, seed)
                except Exception as e:
                    self.failures += 1
                    self.log(f"round failed for {api}: {e!r}")
                    self.log(traceback.format_exc()[:900])
                finally:
                    try:
                        stop(api)
                    except Exception:
                        pass
            done += 1
            seed += 1
            self.rounds += 1
            self.per_api[api] = done
            async with self._lock_report:
                self.write_report()
            self.log(f"{api}: round {done} done; {len(self.verified)} confirmed overall")

    async def run(self, rounds: int, hours: float) -> None:
        deadline = self.started + hours * 3600 if hours else None
        self.write_report()
        await asyncio.gather(*(self._worker(api, rounds, deadline) for api in self.apis))
        self.write_report()
        self.log(f"finished: {self.rounds} rounds across {len(self.apis)} API(s), "
                 f"{len(self.verified)} confirmed findings")
        self.log(f"report: {self.report_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apis", default=",".join(APIS),
                    help=f"comma-separated; available: {', '.join(APIS)}")
    ap.add_argument("--budget", type=float, default=300.0, help="seconds per run")
    ap.add_argument("--rounds", type=int, default=0, help="0 = until --hours elapses")
    ap.add_argument("--hours", type=float, default=0.0, help="0 = until --rounds done")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--jobs", type=int, default=0,
                    help="how many APIs to hunt at once (default: all of them)")
    ap.add_argument("--no-engine", action="store_true",
                    help="skip the default engine pass (semantic findings only)")
    a = ap.parse_args()
    if not a.rounds and not a.hours:
        a.hours = 8.0

    apis = [x.strip() for x in a.apis.split(",") if x.strip()]
    for api in apis:
        if api not in APIS:
            print(f"unknown API {api!r}; known: {', '.join(APIS)}")
            return 2
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, _stop)

    c = Campaign(apis, a.budget, a.out, engine_too=not a.no_engine, jobs=a.jobs)
    c.log(f"starting: apis={apis} jobs={c.jobs} budget={a.budget}s "
          f"{'rounds=' + str(a.rounds) if a.rounds else 'hours=' + str(a.hours)}")
    asyncio.run(c.run(a.rounds, a.hours))
    return 0


if __name__ == "__main__":
    sys.exit(main())
