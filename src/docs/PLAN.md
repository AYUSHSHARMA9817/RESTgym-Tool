# REST League 2027 — Team Plan

Problem: https://seunivr.github.io/RestLeague/2027
**Tool submission deadline: 9 Oct 2026 (AoE).** Register the tool name + authors on EasyChair today (partial submissions are allowed and can be updated until the deadline).

## What we are building
A black-box REST API testing tool in a Docker image. It takes an OpenAPI spec plus `HOST`/`PORT` (and optionally `TIME_BUDGET` in minutes) and sends HTTP requests for one hour. RESTgym's proxy records the traffic, so we don't need to write any test-case output files.

Scoring (per API, averaged over 5 runs, z-score normalized across tools):
- **Unique 5XX** (deduplicated by error message), which gives the Bug Hunter badge
- **Operation coverage**: operations that returned at least one 2XX
- **Code coverage** (methods, statements, branches)
- **AUC** of all of the above over time. Reaching coverage early matters, so get "happy paths" first, then fuzz.

Constraints: 8 cores, 16 GB RAM, an RTX 4090 is available, network access only to the target API, and no remote LLMs.

## Tech choice
**Python 3.11**, `httpx` for HTTP, `prance`/`openapi-spec-validator` for resolving the spec, and plain dataclasses. We skip the LLM in v1. If time allows, a small local model can come in v2 for realistic string values.

## Layout and ownership

```
restleague/
  models.py        # SHARED CONTRACT. Change only with agreement from all 3.
  spec/            # Person A
    parser.py      #   OpenAPI 2/3 -> list[Operation]
    deps.py        #   producer/consumer dependency graph -> operation order
    values.py      #   schema -> concrete values (valid + boundary + invalid)
  engine/          # Person B
    client.py      #   HTTP send, auth headers, timeouts
    pool.py        #   ResourcePool: ids/objects harvested from responses
    strategy.py    #   main loop: nominal phase -> error/mutation phase
    faults.py      #   5XX dedup signature, prioritise ops that crash
  main.py          # Person C: reads env, wires A + B, loops forever
docker/            # Person C
  Dockerfile
  config.sh
  restgym-tool-config.yml
eval/              # Person C
  run_local.sh     #   run tool against RESTgym APIs, collect restats metrics
```

---

### Person A: Spec understanding and input generation
Goal: turn the spec into requests that **get 2XX** as often as possible.
1. `parser.py`: load JSON/YAML, support Swagger 2.0 and OpenAPI 3.x, resolve `$ref`/`allOf`/`oneOf`, and emit `Operation` objects (see `models.py`). Handle base path and `servers` (they get overridden by HOST/PORT).
2. `values.py`: `generate(schema, mode)` where `mode` is one of `VALID | BOUNDARY | INVALID`.
   - VALID: use `example`/`default`/`enum` first, then `format` (date, date-time, email, uuid, uri), then min/max/pattern, then name-based heuristics (`*id`, `email`, `name`, `date`, `phone`, `price`).
   - BOUNDARY/INVALID: min-1, max+1, empty string, huge string, wrong type, null, missing required field, special chars, negative/zero/huge ints. These are the main source of 5XX.
3. `deps.py`: work out which operation produces a resource and which consumes it (POST `/owners` returns `id`, and `GET /owners/{ownerId}` consumes it), using path nesting, parameter-name matching, and response-schema fields. Output a topological order: create operations first, then read/update, then DELETE last.
4. Test the parser on all specs in `RESTgym/apis/*/specifications/`. It must not crash on any of them.

### Person B: Execution engine and testing strategy
Goal: run requests, learn from responses, and **maximise unique 5XX and coverage over time**.
1. `client.py`: send a `Request`, return a `Response`. Use a 10 s timeout, never raise, and handle connection errors by backing off and retrying. Read auth from an `AUTH_HEADER` env var (e.g. `Authorization: Bearer xyz`), and make `POST /login`-style token fetch pluggable.
2. `pool.py`: after every 2XX, harvest ids and objects from the response JSON (and the `Location` header) into a `ResourcePool` keyed by field name and schema. Give A's value generator access to `pool.lookup(param_name, schema)` so it reuses real ids.
3. `strategy.py`: run phases while time remains, then loop:
   - Phase 1 (around the first 10–15% of the budget): nominal requests in A's dependency order, retrying failed operations with different values until each gets a 2XX or hits a retry cap.
   - Phase 2: error and mutation fuzzing. Mutate known-good requests (drop or flip params, type confusion, boundary values, bad ids, malformed JSON body, wrong content type). Weight towards operations that already returned 5XX or have low coverage.
   - Concurrency: about 4–8 workers with asyncio. Don't overload the API: if it dies, we lose the whole run.
4. `faults.py`: compute a 5XX signature (status, operation, normalised message with numbers, ids and timestamps stripped). Track novelty and use it as a bandit reward to choose which operation and mutation to try next.

### Person C: Integration, Docker/RESTgym, evaluation, submission
Goal: make it **run in RESTgym**, measure it, and ship it.
1. `main.py`: read `HOST`, `PORT`, `TIME_BUDGET`, `API`, and the spec path (`/specifications/$API-openapi.json`, or whatever the RESTgym `config.sh` pattern provides), build A's operations, and run B's strategy. **Wire it with stubs on day 1** so the pipeline works end to end immediately.
2. `docker/Dockerfile` plus `config.sh`, following `RESTgym/tools/#tool-template`. The container must run forever (`while true`), and must have no network dependency at runtime (all pip deps installed at build time).
3. Set up RESTgym locally (`./restgym.sh b`, `l`, `v`, `a`). Enable the 5 known competition APIs (kafka-rest-proxy, flight-search, gestao-hospital, notebook-manager, pet-clinic) plus our tool and a baseline (resttestgen or schemathesis). Use a short budget (10 min) during development.
4. Keep a results table (ours vs. baseline: unique 5XX, op coverage, code coverage) after each merge and report the weakest API to A and B.
5. Submission: EasyChair registration (**do this today**), a README with build/run instructions, source code zip, and the Dockerfile. Later (by 4 Dec), draft the 4-page solution paper.

---

## Integration contract
All shared types live in `restleague/models.py`:
- A provides `parse_spec(path) -> list[Operation]`, `order_operations(ops) -> list[Operation]`, and `generate_request(op, pool, mode) -> Request`
- B provides `run(ops, budget_seconds, base_url)`, which uses A's functions plus its own client, pool, and faults modules
- C calls `run()` from `main.py` and packages everything

Each person starts by stubbing their functions to return trivial values, so `main.py` runs end to end from hour 1.

## Timeline (2 days)
| When | A | B | C |
|---|---|---|---|
| Day 1 AM | Parser on all specs | Client + pool | EasyChair registration, RESTgym set up, Docker skeleton running with stubs |
| Day 1 PM | Valid value generation + deps | Nominal phase loop | First real run on 5 APIs, baseline numbers |
| Day 2 AM | Invalid/boundary generation | Mutation phase + 5XX dedup + bandit | Find weak spots, tune budget split, 1 h full runs |
| Day 2 PM | Bug fixes from eval | Robustness (API crash, timeouts) | Final image, README, submit by AoE 9 Oct |

Rules: one git repo, a branch per person, merge to `main` only when `main.py` still runs in Docker.
