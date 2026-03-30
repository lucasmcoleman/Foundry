#!/usr/bin/env bash
# Monitor the gardener data generation progress.
# Usage: bash gardener/monitor.sh

OUTPUT="gardener/gardener_training_data.jsonl"
PARTIAL="${OUTPUT}.partial"
LOG="gardener/generation.log"

echo "=== Gardener Generation Monitor ==="
echo ""

# Check if process is running
PIDS=$(pgrep -f "generate.py.*gardener_training_data" 2>/dev/null)
if [ -n "$PIDS" ]; then
    echo "Status: RUNNING (PID: $PIDS)"
    CLAUDE_PROCS=$(pgrep -fc "claude.*-p" 2>/dev/null || echo 0)
    echo "Active claude CLI processes: $CLAUDE_PROCS"
else
    echo "Status: NOT RUNNING"
fi
echo ""

# Check output files
if [ -f "$OUTPUT" ]; then
    LINES=$(wc -l < "$OUTPUT")
    SIZE=$(du -h "$OUTPUT" | cut -f1)
    echo "Output file: $OUTPUT"
    echo "  Examples: $LINES"
    echo "  Size: $SIZE"
    echo "  GENERATION COMPLETE!"
elif [ -f "$PARTIAL" ]; then
    LINES=$(wc -l < "$PARTIAL")
    SIZE=$(du -h "$PARTIAL" | cut -f1)
    echo "Partial checkpoint: $PARTIAL"
    echo "  Examples so far: $LINES"
    echo "  Size: $SIZE"
else
    echo "No output file yet (results still in memory)"
fi
echo ""

# Check log
if [ -f "$LOG" ] && [ -s "$LOG" ]; then
    echo "Last 5 log lines:"
    tail -5 "$LOG"
fi
