#!/usr/bin/env bash
# Launch the Pattern B (shared SP + custom claim) merchant portal.
# Handles venv creation + dependency install on first run. Usage: ./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8501}"

if [ ! -d .venv ]; then
  echo "→ Creating virtual environment (.venv)…"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "→ Installing dependencies (first run only takes a moment)…"
python3 -m pip install -q --upgrade pip
python3 -m pip install -q -r requirements.txt

echo ""
echo "→ Launching the merchant portal at http://localhost:${PORT}"
echo "  (a browser tab should open; press Ctrl+C here to stop)"
echo ""
exec streamlit run app.py --server.port "${PORT}"
