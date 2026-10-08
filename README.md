# Black-box REST API testing tool — REST League 2027

An automated, black-box testing tool for RESTful APIs, built for
[REST League 2027](https://seunivr.github.io/RestLeague/2027/) (ICSE 2027
Competition Track) and this CS-5187 term project. Given only an API's OpenAPI
specification and its endpoint, the tool sends HTTP requests to **maximise
operation coverage** (endpoints that return 2XX) and **unique 5XX faults**
within a time budget — no access to source code, schema, or logs. It is fully
automated, REST-specific, containerised for RESTgym, and uses no LLM.

## Repository layout

```
src/      the tool (RESTgym-ready) + tests + sample specs      ← code
paper/    the solution/report paper (LaTeX, ACM format)        ← report
```

- Tool details and build/run instructions: [`src/README.md`](src/README.md) and
  [`src/SUBMISSION.md`](src/SUBMISSION.md).

## How it works

```
OpenAPI spec ──▶ parse & resolve ($ref/allOf) ──▶ order operations ──▶ engine
                 (list of Operations)             (creators first,    (budgeted loop)
                                                    DELETE last)
```

- **Spec → requests.** The specification is parsed into operations with every
  `$ref` resolved; operations are ordered so resources are created before they
  are read, updated, or deleted. A value generator fills parameters and bodies
  in three modes — `VALID`, `BOUNDARY`, `INVALID`.
- **Learn & reuse ids.** After every 2XX the engine harvests ids and objects
  from the response (body, `Location` header) into a pool; the generator reuses
  them so nested routes like `POST /owners/{ownerId}/pets` are reachable instead
  of returning 404.
- **Coverage, then fuzz.** Phase 1 drives nominal requests to cover every
  operation; phase 2 mutates known-good requests (missing/boundary/wrong-type
  fields, malformed bodies, stale ids) and runs a systematic injection sweep,
  steered by a bandit toward operations that crash or stay uncovered. A
  slow/timeout back-off keeps one slow endpoint from draining the budget.

## Quick start

Local (no Docker):

```sh
cd src
pip install -r requirements.txt
python -m pytest tests/test_engine.py -q      # engine test suite (18 tests)
python -m tests.check_parser                  # spec parser over 15 sample specs
# run the full tool against any live API (1-minute budget):
HOST=127.0.0.1 PORT=8080 API=pet-clinic TIME_BUDGET=1 \
  SPEC=specs/pet-clinic-openapi.json python -m restleague.main
```

Docker (standalone image, bundles the sample specs):

```sh
cd src
docker build -t restleague-tool -f Dockerfile.standalone .
docker run --rm --network host \
  -e HOST=127.0.0.1 -e PORT=8080 -e API=pet-clinic -e TIME_BUDGET=1 restleague-tool
```

RESTgym runs the tool by setting `HOST`, `PORT`, `API` and `TIME_BUDGET` (minutes)
and mounting the spec at `/specifications/<API>-openapi.json`; see
`src/SUBMISSION.md` for the compliant `Dockerfile`, `config.sh` and
`restgym-tool-config.yml`.

## Status

- Spec parser: 15/15 sample specs parse with 0 unresolved `$ref`.
- Engine test suite: 18/18 pass.
- End-to-end on a live API: full operation coverage with multiple distinct 5XX
  faults at a few hundred requests/second on a single core.

## The paper (`paper/`)

```sh
cd paper
make          # builds paper/main.pdf
make check    # checks TODOs, page limit, undefined refs
```

Every number in the paper comes from a real run of the code in `src/`.
