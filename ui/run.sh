#!/bin/bash
# Launch the Pipeline UI (FastAPI + WebSocket log streaming)
#
# Usage: ./run.sh [port]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${SCRIPT_DIR}/../.venv/bin"

if [ ! -f "$VENV/uvicorn" ]; then
    echo "ERROR: uvicorn not found in $VENV"
    echo "Install with: pip install uvicorn"
    exit 1
fi

PORT="${1:-7865}"

export HSA_ENABLE_SDMA=0
export PYTORCH_HIP_ALLOC_CONF="backend:native,expandable_segments:True"
export TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1
export HF_HUB_ENABLE_HF_TRANSFER=1

# Default to loopback. To expose on the LAN set FOUNDRY_UI_HOST=0.0.0.0 AND
# FOUNDRY_API_KEY=... — running the pipeline grants shell-equivalent host access.
HOST="${FOUNDRY_UI_HOST:-127.0.0.1}"
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ] && [ "$HOST" != "::1" ] && [ -z "${FOUNDRY_API_KEY:-}" ]; then
    echo "ERROR: refusing to bind ${HOST} without FOUNDRY_API_KEY set (unauthenticated, shell-equivalent access)."
    echo "Set FOUNDRY_API_KEY, or bind 127.0.0.1."
    exit 1
fi

echo "Starting Pipeline UI on http://${HOST}:${PORT}"
exec "$VENV/uvicorn" app:app --host "$HOST" --port "$PORT" --app-dir "$SCRIPT_DIR"
