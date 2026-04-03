#!/bin/bash
# Activate the pipeline training environment
#
# Usage: source activate.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate venv
if [ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: Virtual environment not found at $SCRIPT_DIR/.venv"
    echo "Create it with: python3 -m venv $SCRIPT_DIR/.venv"
    return 1 2>/dev/null || exit 1
fi
source "$SCRIPT_DIR/.venv/bin/activate"

# Add core modules to Python path
export PYTHONPATH="$SCRIPT_DIR/core:${PYTHONPATH:-}"

# AMD ROCm optimizations for Strix Halo (gfx1151)
export HSA_ENABLE_SDMA=0
export PYTORCH_HIP_ALLOC_CONF="backend:native,expandable_segments:True"
export TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1

# HuggingFace config
export HF_HUB_ENABLE_HF_TRANSFER=1
if [ -z "$HF_TOKEN" ] && [ -f ~/.cache/huggingface/token ]; then
    export HF_TOKEN="$(tr -d '[:space:]' < ~/.cache/huggingface/token)"
fi

echo "Pipeline environment activated (Python $(python --version 2>&1 | cut -d' ' -f2))"
python -c "import torch; print(f'PyTorch {torch.__version__} | GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"none\"}')" 2>/dev/null

# Start the Pipeline UI in the background
UI_PORT="${PIPELINE_UI_PORT:-7865}"
if ! ss -tlnp 2>/dev/null | grep -q ":${UI_PORT} "; then
    if [ -x "$SCRIPT_DIR/ui/run.sh" ]; then
        "$SCRIPT_DIR/ui/run.sh" "$UI_PORT" > "$SCRIPT_DIR/ui/ui.log" 2>&1 &
        PIPELINE_UI_PID=$!
        sleep 1
        if kill -0 "$PIPELINE_UI_PID" 2>/dev/null; then
            echo "Pipeline UI started on http://localhost:${UI_PORT} (pid $PIPELINE_UI_PID)"
        else
            echo "WARNING: Pipeline UI failed to start. Check $SCRIPT_DIR/ui/ui.log"
        fi
    else
        echo "WARNING: ui/run.sh not found or not executable"
    fi
else
    echo "Pipeline UI already running on http://localhost:${UI_PORT}"
fi
