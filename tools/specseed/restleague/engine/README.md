# Execution engine

The engine sends requests to the target API, learns from the responses, and
maximises unique 5XX faults and operation coverage within a time budget. It is
self-contained and uses the spec component (`restleague/spec/`) to turn an
OpenAPI specification into requests; if that package is missing it falls back to
a built-in parser/generator so it still runs.

## Modules (`restleague/engine/`)

| File | What it does |
|---|---|
| `client.py` | Async HTTP: 10 s timeout, never raises, retry + backoff on transport errors, liveness tracking. Pluggable auth from env (`AUTH_HEADER`, or a login-token flow that refreshes on 401). |
| `pool.py` | `ResourcePool`: after every 2XX, harvests ids/objects from the JSON body, `Location` header and accepted request bodies. `lookup(param, schema, op)` returns real ids so nested routes are reachable. |
| `faults.py` | `FaultTracker`: 5XX signature = (status, operation, normalised message); dedup mode configurable via `FAULT_DEDUP`. Plus a UCB bandit to steer op/mutation choice. |
| `mutations.py` | Schema-aware mutation operators (missing/wrong-type/boundary/malformed) plus vulnerability-detection probes and a systematic injection sweep. |
| `strategy.py` | **Entry point.** `run(ops, budget_seconds, base_url, …) -> RunResult`. Phase 1: nominal requests in dependency order until each op gets a 2XX. Phase 2: bandit-guided fuzzing, with a slow/timeout back-off so one slow endpoint can't eat the budget. ~6 asyncio workers. |
| `fallback.py` | Built-in parser/generator used only when the spec package can't be imported. |

## Entry point

```python
from restleague.engine.strategy import run
result = run(ops, budget_seconds=3600, base_url="http://HOST:PORT")
print(result.unique_5xx, result.operations_covered, "/", result.operations_total)
```

`run` auto-loads the spec component via `from restleague.spec import parser, values`
and `from restleague.spec.deps import order_operations`.

## Env knobs
`REQUEST_TIMEOUT` (s, default 10), `SLOW_THRESHOLD` (s, default 3),
`FAULT_DEDUP` (`op_message` | `message` | `op_status`), plus the auth vars read
by `client.py`.
