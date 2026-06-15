#!/usr/bin/env bash
# Launch the Streamlit UI, clearing the bytecode cache first to avoid
# stale-module ImportErrors after big code changes.
#
# Usage:
#   ./run.sh             # headless on port 8501
#   ./run.sh 8502        # headless on a custom port

set -euo pipefail

cd "$(dirname "$0")"
PORT="${1:-8501}"

# Always start from a clean bytecode cache.
rm -rf __pycache__

# Activate the venv if it exists.
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

exec streamlit run app.py \
  --server.headless true \
  --server.port "$PORT" \
  --browser.gatherUsageStats false
