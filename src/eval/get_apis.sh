#!/bin/sh
# Fetch the RESTgym API repositories used in the live evaluation, at the commits we used.
# Usage: sh eval/get_apis.sh [TARGET_DIR]   (default: ../apis, i.e. next to src/)
set -e
DIR="${1:-$(dirname "$0")/../../apis}"
mkdir -p "$DIR"
fetch() {  # name commit
  if [ ! -d "$DIR/$1" ]; then
    git clone -q "https://github.com/restgym/$1-api.git" "$DIR/$1"
  fi
  git -C "$DIR/$1" checkout -q "$2"
  echo "$1 at $(git -C "$DIR/$1" rev-parse --short HEAD)"
}
fetch pet-clinic e8250dba178bb995aab80313f506a571a0393aaa
fetch gestao-hospital d4cb6c730ec0ac03d1092a399f6f31d7cf5bc1be
