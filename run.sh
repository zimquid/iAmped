#!/usr/bin/env bash
# Launch iAmped. Creates the venv on first run.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment…"
  "${PYTHON:-python3}" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

exec ./.venv/bin/python -m iamped.app "$@"
