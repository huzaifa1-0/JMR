#!/usr/bin/env bash
# Job Market Research Platform - macOS / Linux launcher
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "First-run setup. This takes a few minutes, once only."
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install --upgrade pip
  pip install -r requirements.txt
  echo "Setup complete."
else
  source .venv/bin/activate
fi

python -m backend.app.main
