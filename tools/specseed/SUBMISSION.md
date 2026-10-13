# Submission & RESTgym packaging

A black-box REST API testing tool for REST League 2027. It turns an OpenAPI
specification into requests, drives the API to maximise operation coverage
(2XX) and unique 5XX faults within a time budget, and is fully automated,
black-box, REST-specific, and containerised. No LLM is used.

## Files in this tool directory (`src/`)

```
restleague/              tool source (models, spec/, engine/, main.py)
specs/                   15 sample OpenAPI specs (for local testing)
tests/                   test suite
requirements.txt         runtime deps (httpx; PyYAML for YAML specs)
config.sh                RESTgym per-API setup (sets defaults, logs)
Dockerfile               RESTgym-compliant image (built by RESTgym)
Dockerfile.standalone    self-contained image for local testing
restgym-tool-config.yml  enabled: true
```

## How RESTgym runs it (verified)

RESTgym sets `HOST`, `PORT`, `API` (and optionally `TIME_BUDGET` in minutes),
bakes the specs into `/specifications/<API>-openapi.json`, then runs:

```
sh /tool/config.sh && while true; do python3 -m restleague.main; done
```

`restleague/main.py` reads those variables directly:
`base_url = http://HOST:PORT`, spec = `/specifications/$API-openapi.json`,
budget = `TIME_BUDGET` minutes (default 60). The container needs no network at
runtime except the target API; all pip installs happen at build time.

**One thing to confirm before building:** in `Dockerfile`, `ARG TOOL_NAME`
must equal the directory name this tool is placed under in `RESTgym/tools/`
(and ideally the name registered on EasyChair). Default is `specseed`.

## Optional auth (for protected APIs)
The engine reads auth material from the environment if provided:
`AUTH_HEADER="Authorization: Bearer <token>"`, or a login flow
(`AUTH_LOGIN_PATH`, `AUTH_LOGIN_BODY`, `AUTH_TOKEN_FIELD`, `AUTH_TOKEN_HEADER`,
`AUTH_TOKEN_PREFIX`).

## Local testing

Without Docker:
```bash
pip install -r requirements.txt
python -m pytest tests/test_engine.py -q      # 18 engine tests
python -m tests.check_parser                  # spec parser on 15 specs
# full pipeline against any running API (1-minute budget):
HOST=127.0.0.1 PORT=8080 API=pet-clinic TIME_BUDGET=1 \
  SPEC=specs/pet-clinic-openapi.json python -m restleague.main
```

With Docker (standalone image, bundles the sample specs):
```bash
docker build -t restleague-tool -f Dockerfile.standalone .
docker run --rm --network host \
  -e HOST=127.0.0.1 -e PORT=8080 -e API=pet-clinic -e TIME_BUDGET=1 \
  restleague-tool
```

## Resources
Runs within 8 cores / 16 GB. ~6 async workers by default; a slow/timeout
back-off keeps one slow endpoint from draining the budget.

## Verified
- Spec parser: 15/15 specs parse.
- Engine test suite: 18/18 pass.
- Container runtime contract (config.sh + main.py via HOST/PORT/API/SPEC/
  TIME_BUDGET, spec at /specifications/<API>-openapi.json): runs the full
  pipeline and reports coverage + unique 5XX.
- End-to-end on a live mock: 7/7 operations covered, ~10 unique 5XX, ~330 req/s.
