# SpecSeed

SpecSeed is a first-prototype automated black-box REST API testing tool for
REST League 2027. It uses an OpenAPI specification, generated requests, HTTP
feedback, resource reuse, operation ordering, and request mutations to explore
REST APIs.

## Requirements

- Docker
- Linux, macOS, or Windows with WSL2 and Docker Desktop WSL integration
- A RESTgym checkout

The tool does not require source-code access, database-schema access, internal
logs, or a remote LLM.

## Installation

Place the submitted `specseed` directory under the RESTgym repository:

```text
restgym/
└── tools/
    └── specseed/
```

The directory must contain:

```text
tools/specseed/
├── Dockerfile
├── config.sh
├── restgym-tool-config.yml
├── requirements.txt
├── restleague/
├── tests/
└── docs/
```

The RESTgym repository supplies the API containers and mounts the API
specifications for each experiment.

## Docker build

Run this command from the RESTgym repository root:

```bash
docker build -f tools/specseed/Dockerfile -t restgym-specseed .
```

The Dockerfile installs all Python dependencies during image build. No package
installation is required while the tool is testing an API.

## RESTgym execution

From the RESTgym repository root:

```bash
chmod +x restgym.sh
./restgym.sh b
./restgym.sh l
./restgym.sh v
./restgym.sh a
```

The commands respectively build images, launch experiments, verify raw
experiment data, and analyze verified results.

RESTgym prompts for the number of repetitions. The official competition uses
five repetitions and a one-hour budget per API. Use a shorter budget during
local smoke tests.

## Runtime environment

RESTgym provides these environment variables to the tool:

| Variable | Meaning |
|---|---|
| `HOST` | Hostname of the target API or RESTgym proxy |
| `PORT` | Port of the target API or RESTgym proxy |
| `API` | RESTgym API slug |
| `TIME_BUDGET` | Testing budget in minutes |

The tool sends requests to:

```text
http://$HOST:$PORT
```

The selected OpenAPI specification is read from:

```text
/specifications/$API-openapi.json
```

RESTgym mounts this specification into the tool container. The tool does not
hardcode any of the benchmark API implementations.

## Authentication

Optional authentication can be configured through environment variables:

```text
AUTH_HEADER
AUTH_LOGIN_PATH
AUTH_LOGIN_BODY
AUTH_TOKEN_FIELD
AUTH_TOKEN_HEADER
AUTH_TOKEN_PREFIX
```

These are only needed for APIs requiring authentication.

## Competition metrics

SpecSeed sends the HTTP requests. RESTgym and its independent Restats
infrastructure record the traffic and compute:

- Unique 5XX fault detection
- Operation coverage
- Method, statement, and branch coverage
- Testing-efficiency AUC

SpecSeed does not require access to API source code or internal coverage data.

## Local checks

From `tools/specseed/`, the following checks can be run:

```bash
python -m tests.check_parser
python -m tests.check_values
python -m tests.check_pool
python -m pytest tests/test_engine.py -q
```

## Tool maturity

SpecSeed is a working first prototype. It has been packaged as a RESTgym
Docker tool and validated through a local build, a smoke experiment, RESTgym
data verification, and result analysis. It has not yet been field-tested in
the organizers' final competition environment.

## License

SpecSeed is released under the Apache License 2.0; see [`LICENSE`](LICENSE).

## Tool manifest

[`restgym-tool-manifest.yaml`](restgym-tool-manifest.yaml) holds the metadata RESTgym
needs to run the tool: id `specseed`, OpenAPI 3.0 JSON specifications, a time budget,
no LLM, no GPU and no network access except the API under test.

## Authors

Ayush Sharma, Sayan Mandal and Biraj Sarkar (Indian Institute of Technology Guwahati).
