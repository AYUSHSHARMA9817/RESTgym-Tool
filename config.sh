#!/bin/sh
set -u

# RESTgym provides HOST, PORT, API, and TIME_BUDGET for each session.
# main.py consumes these variables directly.
HOST="${HOST:-localhost}"
PORT="${PORT:-8080}"
TIME_BUDGET="${TIME_BUDGET:-60}"
export HOST PORT TIME_BUDGET

echo "[config] target=http://${HOST}:${PORT} API=${API:-?} TIME_BUDGET=${TIME_BUDGET}min"
