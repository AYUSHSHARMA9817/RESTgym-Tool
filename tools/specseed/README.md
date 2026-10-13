# SpecSeed — REST League 2027 tool

This directory is the RESTgym deployment package for SpecSeed, a black-box
REST API testing tool. RESTgym builds it from the repository root with:

```sh
docker build -f tools/specseed/Dockerfile -t restgym-specseed .
```

At runtime RESTgym provides `HOST`, `PORT`, `API`, and `TIME_BUDGET`. The
container reads the selected specification from
`/specifications/$API-openapi.json` and sends requests only to
`http://$HOST:$PORT`. The container is intentionally kept alive for the
duration of the RESTgym session.

The `restgym-tool-config.yml` file enables this tool in RESTgym. The official
RESTgym pipeline records HTTP interactions and computes fault detection,
operation coverage, code coverage, and efficiency metrics independently.

Our team builds a black-box REST API testing tool for
[REST League 2027](https://seunivr.github.io/RestLeague/2027/) in three parts:
the **spec component**, the **engine** that sends requests, and this RESTgym
packaging. SpecSeed turns an OpenAPI specification into requests and executes
them through the integrated engine:

| Step | File | What it does |
|---|---|---|
| 1 Parse and resolve | `restleague/spec/parser.py` | OpenAPI 3 file → list of `Operation`s, every `$ref` resolved, `allOf` merged, cycles cut |
| 2 Order operations | `restleague/spec/deps.py` | producer/consumer resource keys → creators first, `DELETE` last |
| 3 Generate values | `restleague/spec/values.py` | `generate_request(op, pool, mode)` with modes `VALID`, `BOUNDARY`, `INVALID` |
| 4 Reuse ids | `restleague/spec/values.py` | asks the engine's pool: `pool.lookup(name, schema, op)`, `pool.lookup_object(resource)` |

The shared types are in `restleague/models.py` (the team's common interface;
see `docs/INTERFACE.md`). `restleague/main.py` is the RESTgym entrypoint and
`restleague/engine/strategy.py` runs the testing strategy.

## Layout

```
restleague/        the component (models, spec/parser.py, spec/deps.py, spec/values.py)
tests/             parser ladder, real-spec checks, value checks, pool check
specs/             the 15 OpenAPI specifications used (from the RESTgym API repositories)
eval/              evaluation: baseline generator, schema checker, pool, HTTP, runners, summary
docs/              team plan and the engine interface
```

## Requirements (exact versions used for the report)

- Python 3.14.4 (standard library only; no `pip install` needed)
- Docker 29.8.2, only for the live evaluation, with the images
  `eclipse-temurin:17-jre` (to run the API jars) and `mongo:6` (for gestao-hospital)
- `texlive/texlive:latest` Docker image (pulled 2026-10-08) to build the paper
- Machine: Intel Core 7 240H, 16 logical CPUs, 8 GB RAM visible to Docker,
  Linux 6.18 (WSL2)

## Tests

Run from this folder:

```sh
python3 -m tests.parser_levels      # 13 small specs, one parser rule each
python3 -m tests.check_parser       # all specs in specs/: operation counts, no $ref left
python3 -m tests.check_values       # 30 requests per operation and mode; VALID values must fit the schema
python3 -m tests.check_pool         # real ids from a stand-in pool reach the request
```

## Reproducing the paper's numbers

All commands run from this folder and write into `../paper/data/`.

```sh
# 1. offline evaluation (no API needed, < 1 minute):
#    parser.csv, conformance.csv, cost.csv
python3 -m eval.run_offline ../paper/data

# 2. live evaluation (Docker; about 20 minutes):
#    fetch the two APIs at the commits we used, then 5 repetitions x 4 configurations x 2 APIs
sh eval/get_apis.sh /path/to/apis
RL_APIS_DIR=/path/to/apis python3 -m eval.run_live ../paper/data 5 pet-clinic gestao-hospital
RL_APIS_DIR=/path/to/apis python3 -m eval.motivating ../paper/data

# 3. tables and number macros used by the paper:
#    ../paper/tables/parser.tex, ../paper/tables/live.tex, ../paper/data/numbers.tex
python3 -m eval.summarize ../paper/data ../paper

# 4. build the paper
cd ../paper && docker run --rm -u $(id -u):$(id -g) -v "$PWD":/w -w /w texlive/texlive:latest make
```

The live runner restarts each API from a fresh container before every run, and gives
every configuration the same budget of 20 requests per operation. The four
configurations are:

| Configuration | Values | Id reuse (pool) | Dependency order |
|---|---|---|---|
| `baseline` | type and enum only (`eval/baseline.py`) | no | no |
| `values` | SpecSeed | no | no |
| `values+pool` | SpecSeed | yes | no |
| `full` | SpecSeed | yes | yes |

`eval/pool.py` is a small stand-in for the engine's resource pool with the same
interface; the engine itself is not part of this folder.

## Using the component from the engine

```python
from restleague.models import Mode
from restleague.spec.parser import parse_spec
from restleague.spec.deps import order_operations
from restleague.spec.values import generate_request

ops = order_operations(parse_spec("specs/pet-clinic-openapi.json"))
req = generate_request(ops[0], pool, Mode.VALID)   # pool: any object with lookup(name, schema, op)
```

## Sources of the specifications

The files in `specs/` are copied unchanged from the RESTgym API repositories
(`https://github.com/SeUniVr/RESTgym`, folder `apis/*/specifications/`, and
`https://github.com/restgym/<api>-api` for the five REST League APIs).
