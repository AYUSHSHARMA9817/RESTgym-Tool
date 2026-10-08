#!/bin/sh
# RESTgym runs this once before the tool starts.
# RESTgym provides HOST, PORT, API (which spec to test) and optionally
# TIME_BUDGET (minutes). The API specifications are baked into /specifications/
# at build time as "<API>-openapi.json".
#
# Our entrypoint (restleague.main) reads these variables directly:
#   HOST, PORT        -> base URL  http://HOST:PORT
#   API / SPEC        -> spec path /specifications/$API-openapi.json
#   TIME_BUDGET       -> minutes (defaults to 60)
# so there is nothing to rewrite; we just set safe defaults and log.
HOST="${HOST:-localhost}"
PORT="${PORT:-8080}"
export HOST PORT
echo "[config] target=http://$HOST:$PORT  API=${API:-?}  TIME_BUDGET=${TIME_BUDGET:-60}min"
