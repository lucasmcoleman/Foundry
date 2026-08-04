#!/usr/bin/env bash
# Run a large model as a DETACHED, memory-capped systemd transient unit.
#
# WHY THIS EXISTS (incident 2026-08-04, second occurrence):
#   Bash commands launched by an agent run inside the agent's own systemd scope
#   (tmux-spawn-<uuid>.scope). When ds4 held 81 GiB and the box ran out of
#   memory, the kernel OOM-killed ds4 and systemd then tore down the WHOLE
#   SCOPE -- killing `claude`, `bun`, `uv`, and `python` with it:
#
#     tmux-spawn-1e620528-....scope,task=ds4,pid=272393   <- kernel kills ds4
#     tmux-spawn-1e620528-....scope: Killing process 282359 (claude) SIGKILL
#     tmux-spawn-1e620528-....scope: Failed with result 'oom-kill'
#
#   A model run in its own system-scope transient unit cannot do that: the
#   cgroup OOM-killer kills only the unit. Verified on this box -- a 2 GiB
#   allocation under MemoryMax=200M died at its ceiling and nothing else
#   was touched.
#
# WHY SYSTEM SCOPE, NOT --user:
#   user@1000.service itself died in the same incident, which breaks
#   `systemd-run --user` entirely. A system-scope unit is independent of the
#   user manager, the login session, and the tmux scope.
#
# Usage:
#   run-model-detached.sh --name ds4-probe [--mem 95G] [--log FILE] -- <cmd> [args...]
#   run-model-detached.sh --status ds4-probe
#   run-model-detached.sh --wait   ds4-probe        # block until it finishes
#   run-model-detached.sh --logs   ds4-probe
#   run-model-detached.sh --stop   ds4-probe
set -uo pipefail

UNIT_PREFIX="model-"
DEFAULT_MEM="95G"          # 121 GiB box: leaves ~26 GiB for OS + agents
RESERVE_GIB=12             # refuse to launch if less than this would remain

die() { echo "run-model-detached: $*" >&2; exit 1; }
unit_of() { echo "${UNIT_PREFIX}$1"; }

cmd_status() {
  local u; u=$(unit_of "$1")
  systemctl is-active "$u" 2>/dev/null
  systemctl show "$u" -p Result -p ExecMainStatus -p MemoryPeak 2>/dev/null
  echo "--- last log lines:"
  journalctl -u "$u" --no-pager -n 15 2>/dev/null
}

cmd_wait() {
  local u; u=$(unit_of "$1")
  echo "waiting on $u ..."
  while systemctl is-active "$u" >/dev/null 2>&1; do sleep 10; done
  echo "--- finished. result:"
  # NOTE: with --collect, `systemctl show` may report success after reaping.
  # The journal is the source of truth for how it actually ended.
  journalctl -u "$u" --no-pager -n 5 2>/dev/null
}

cmd_logs() { journalctl -u "$(unit_of "$1")" --no-pager -n "${2:-80}" 2>/dev/null; }
cmd_stop() { sudo -n systemctl stop "$(unit_of "$1")" 2>/dev/null; echo "stopped $(unit_of "$1")"; }

case "${1:-}" in
  --status) shift; cmd_status "$@"; exit $? ;;
  --wait)   shift; cmd_wait   "$@"; exit $? ;;
  --logs)   shift; cmd_logs   "$@"; exit $? ;;
  --stop)   shift; cmd_stop   "$@"; exit $? ;;
esac

NAME=""; MEM="$DEFAULT_MEM"; LOGFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --mem)  MEM="$2";  shift 2 ;;
    --log)  LOGFILE="$2"; shift 2 ;;
    --)     shift; break ;;
    *)      die "unknown option: $1 (did you forget -- before the command?)" ;;
  esac
done
[[ -n "$NAME" ]] || die "--name is required"
[[ $# -gt 0 ]]   || die "no command given after --"

UNIT=$(unit_of "$NAME")

# --- Preflight: is there actually room? -------------------------------------
# GTT-pinned pages leave MemAvailable, so this is the number that matters.
avail_gib=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)
mem_gib=${MEM%[Gg]}
if (( avail_gib < mem_gib + RESERVE_GIB )); then
  die "PREFLIGHT FAILED: MemAvailable ${avail_gib} GiB < cap ${mem_gib} GiB + ${RESERVE_GIB} GiB reserve.
       Free memory first (stop llama-swap, unload LM Studio, wait for a running
       model to finish) or lower --mem. Refusing to start a run that would
       drive the box into an OOM cascade."
fi

# Concurrency guard: never two big models at once.
if systemctl is-active "$UNIT" >/dev/null 2>&1; then
  die "$UNIT is already active. Use --status/--wait, or --stop it first."
fi
running=$(systemctl list-units --type=service --state=running --no-legend "${UNIT_PREFIX}*" 2>/dev/null | wc -l)
if (( running > 0 )); then
  systemctl list-units --type=service --state=running --no-legend "${UNIT_PREFIX}*" 2>/dev/null >&2
  die "another ${UNIT_PREFIX}* unit is already running (see above). Serialize big-model runs."
fi

[[ -n "$LOGFILE" ]] || LOGFILE="/server/programming/Foundry/output/_model_runs/${NAME}.log"
mkdir -p "$(dirname "$LOGFILE")"

echo "run-model-detached: unit=$UNIT mem=$MEM avail=${avail_gib}GiB log=$LOGFILE"
echo "  cmd: $*"

# --collect  : auto-reap so a failed unit does not linger in `systemctl --failed`
# MemorySwapMax=0 : swap thrash was a major contributor to the 2026-08-04 cascade
#                   (32 GiB swap fully consumed before the kill)
sudo -n systemd-run \
  --unit="$UNIT" \
  --collect \
  --property=MemoryMax="$MEM" \
  --property=MemorySwapMax=0 \
  --property=OOMPolicy=stop \
  --property=WorkingDirectory="$PWD" \
  --property=Environment=LD_LIBRARY_PATH=/home/lucas/lib-override \
  --property=StandardOutput="append:$LOGFILE" \
  --property=StandardError="append:$LOGFILE" \
  --uid=lucas \
  "$@" || die "systemd-run failed to start $UNIT"

echo "started. follow with:  $0 --logs $NAME   |   block with: $0 --wait $NAME"
