#!/bin/bash
# Print (and optionally run) the recommended llama-server command for a GGUF.
#
# Usage: ./scripts/serve.sh <gguf> [--port N] [--no-mtp]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${REPO_ROOT}/.venv/bin"
PYTHON="$VENV/python"

if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

cd "$REPO_ROOT" && exec "$PYTHON" -m core.serving "$@"
