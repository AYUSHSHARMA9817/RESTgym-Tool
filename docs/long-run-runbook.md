# Long-run runbook (multi-hour unattended campaigns)

What to do for a 1-hour+ run, and what to watch.

## 1. Bring the target up (crAPI)

```bash
# starts the stack (chatbot-stub + health-gated) via the committed harness
RL_CRAPI_COMPOSE=/path/to/crapi/docker-compose.yml \
  python -c "from eval.apis import start; print(start('crapi'))"
```

## 2. Pre-flight (gate the run)

```bash
python -m eval.preflight --apis crapi && echo GO
```
Checks the gateway answers, login works, and there's disk headroom. Exits
non-zero (no `GO`) if anything's off.

## 3. Start the campaign

```bash
# spec-only (baseline) — set a graph cap as a safety net for a long run
RKG_MAX_NODES=100000 RL_CRAPI_COMPOSE=/path/.../docker-compose.yml \
  python -m eval.campaign --apis crapi --hours 1 --no-engine \
  --out runs/crapi-$(date +%F) 2>&1 | tee runs/crapi.log

# with the LLM planner (treatment arm) — add:
#   LLM_PLANNER=1   (needs `pip install anthropic` + credentials)
```

## 4. Watch it (separate terminal)

```bash
bash eval/watch.sh eval.campaign 3000 15   # pattern, alert-MB, interval-s
```
Prints fuzzer RSS + crAPI container memory each interval; prints a line starting
`ALERT` if RSS crosses the threshold or the process dies.

## What to watch for

- **Fuzzer memory (RSS).** The knowledge graph is the main grower. The 30s log
  line now ends with `rss=…MB rkg=<nodes>`; if `rkg` climbs into the hundreds of
  thousands and RSS with it, the `RKG_MAX_NODES` cap (step 3) bounds it —
  `evicted=<n>` then appears in the line. The request/value pool is already
  bounded, so it's not a suspect.
- **crAPI stores.** `fast_verify` means no restart, so mongo/postgres grow the
  whole run. `watch.sh` shows their memory; keep an eye on disk too. If they get
  huge, restart the stack between runs (`stop('crapi')` then `start('crapi')`).
- **5XX count** (`unique5xx` in the log) will keep rising — those are real
  server crashes, expected on crAPI; they don't indicate a tool fault.
- **The campaign is resumable.** Findings are written as they're found and the
  report is regenerated each round, so a crash or Ctrl-C loses nothing already
  confirmed.

## Knobs

| Env | Effect | Default |
|---|---|---|
| `RKG_MAX_NODES` | soft cap on graph entities (oldest leaves evicted) | `0` (unbounded) |
| `LLM_PLANNER` | enable the LLM prerequisite planner | off |
| `REQUEST_TIMEOUT` | per-request HTTP timeout (s) | `10` |
| `ORACLE_CHECKS_PER_OP` | oracle re-checks per op per window | `3` |
