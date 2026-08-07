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
#   run-model-detached.sh --name small-job --allow-concurrent --mem 24G -- <cmd>
#       ^ small job that must not queue behind a running big-model unit
#   run-model-detached.sh --status ds4-probe
#   run-model-detached.sh --wait   ds4-probe        # block until it finishes
#   run-model-detached.sh --logs   ds4-probe
#   run-model-detached.sh --stop   ds4-probe
set -uo pipefail

UNIT_PREFIX="model-"
# MemoryMax is a RUNAWAY BACKSTOP, not a working cap. GTT/GPU-pinned pages are
# not charged to the cgroup: a probe that had 80.76 GiB resident reported
# `547.7M memory peak` to systemd. So a snug cap does nothing useful, while a
# too-snug one kills legitimate runs. Set it near total RAM so it only catches
# a true runaway; real protection comes from (a) the separate unit, (b) the
# preflight below, (c) OOMScoreAdjust making the model the kernel's first
# victim, and (d) earlyoom.
DEFAULT_MEM="110G"
RESERVE_GIB=8              # empirical: the 2026-08-04 smoke test succeeded with 6.7 GiB slack

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

NAME=""; MEM="$DEFAULT_MEM"; LOGFILE=""; PROTECT=0; ALLOW_CONCURRENT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)    NAME="$2"; shift 2 ;;
    --mem)     MEM="$2";  shift 2 ;;
    --log)     LOGFILE="$2"; shift 2 ;;
    # --protect: this run is LONG and NOT resumable, so it must survive a
    # global OOM rather than absorb it.
    #
    # WHY THIS EXISTS (2026-08-05): the default OOMScoreAdjust=1000 below is
    # correct for a disposable inference run -- kill the model, spare the box.
    # Applied to a 7-hour QAT job with no checkpointing it is exactly
    # backwards: it guarantees the most expensive, least recoverable job is the
    # kernel's FIRST victim. An ffmpeg transcode drove the box OOM and the
    # kernel duly killed a 7h training run while 1 GB containers survived,
    # because I had volunteered it. --protect inverts that: negative adj plus a
    # MemoryMin reservation so reclaim pressure goes elsewhere first.
    --protect) PROTECT=1; shift ;;
    # --allow-concurrent: opt this run OUT of the big-model serialization guard.
    #
    # The guard below exists so two multi-GB models never share 121 GB of
    # unified memory. A SMALL companion job -- a 0.5B control experiment, a
    # tokenizer pass -- is not what it protects against, and blocking it behind
    # a multi-day search just pushes it back into the agent's own scope, which
    # is the exact failure this whole script exists to prevent.
    #
    # The waiver is deliberately narrow: it drops the "is another model-* unit
    # running" check and NOTHING else. The unit still gets its own cgroup, the
    # MemAvailable preflight still runs, and an explicit --mem is REQUIRED --
    # a run that skips the queue must state its own ceiling rather than inherit
    # the runaway backstop meant for a box-sized model.
    --allow-concurrent) ALLOW_CONCURRENT=1; shift ;;
    --)        shift; break ;;
    *)         die "unknown option: $1 (did you forget -- before the command?)" ;;
  esac
done
[[ -n "$NAME" ]] || die "--name is required"
[[ $# -gt 0 ]]   || die "no command given after --"

UNIT=$(unit_of "$NAME")

# --- Preflight: is there actually room? -------------------------------------
# Check against what the MODEL needs, not against --mem (which is only a
# runaway backstop -- see the DEFAULT_MEM comment). The requirement is derived
# from the actual model file named by -m/--model: ds4 reports
# "resident model <file size>" plus ~1 GiB of KV/buffers, and prefill working
# buffers scale with the context, so add a margin.
avail_gib=$(awk '/MemAvailable/ {printf "%d", $2/1024/1024}' /proc/meminfo)

model_file=""
prev=""
for tok in "$@"; do
  case "$prev" in -m|--model) model_file="$tok"; break ;; esac
  case "$tok" in --model=*) model_file="${tok#--model=}"; break ;; esac
  prev="$tok"
done

need_gib=0
if [[ -n "$model_file" && -f "$model_file" ]]; then
  model_gib=$(( $(stat -c%s "$model_file") / 1024 / 1024 / 1024 ))
  need_gib=$(( model_gib + 2 ))       # ds4 measured: 80.76 GiB model -> 81.29 GiB total (KV 0.46 + buffers 0.06)
  echo "run-model-detached: model $(basename "$model_file") = ${model_gib} GiB -> needs ~${need_gib} GiB"
fi

if (( avail_gib < need_gib + RESERVE_GIB )); then
  die "PREFLIGHT FAILED: MemAvailable ${avail_gib} GiB < model need ${need_gib} GiB + ${RESERVE_GIB} GiB reserve.
       Free memory first: stop llama-swap, unload LM Studio, finish any running
       subagent fan-out (concurrent pytest suites plus an 81 GiB model is what
       exhausted the box on 2026-08-04). Refusing to start."
fi

# Concurrency guard: never two big models at once.
if systemctl is-active "$UNIT" >/dev/null 2>&1; then
  die "$UNIT is already active. Use --status/--wait, or --stop it first."
fi
if (( ALLOW_CONCURRENT )); then
  # Waived on purpose (see --allow-concurrent above). The trade is explicit:
  # skip the queue, state your own ceiling.
  [[ "$MEM" != "$DEFAULT_MEM" ]] || die \
    "--allow-concurrent requires an explicit --mem. The serialization guard is
       waived for this run; the memory ceiling must not be. Pass a cap sized to
       what this job actually needs (e.g. --mem 24G)."
  echo "run-model-detached: --allow-concurrent -- skipping the big-model" \
       "serialization guard, capped at $MEM"
  systemctl list-units --type=service --state=running --no-legend "${UNIT_PREFIX}*" 2>/dev/null >&2
else
  running=$(systemctl list-units --type=service --state=running --no-legend "${UNIT_PREFIX}*" 2>/dev/null | wc -l)
  if (( running > 0 )); then
    systemctl list-units --type=service --state=running --no-legend "${UNIT_PREFIX}*" 2>/dev/null >&2
    die "another ${UNIT_PREFIX}* unit is already running (see above). Serialize big-model runs.
       If this run is SMALL and genuinely safe alongside it, pass
       --allow-concurrent --mem <cap>."
  fi
fi

[[ -n "$LOGFILE" ]] || LOGFILE="/server/programming/Foundry/output/_model_runs/${NAME}.log"
mkdir -p "$(dirname "$LOGFILE")"

echo "run-model-detached: unit=$UNIT mem=$MEM avail=${avail_gib}GiB log=$LOGFILE"
echo "  cmd: $*"

# Shield the agent session from the OOM killer for the duration of the run.
#
# WHY (2026-08-05, the SECOND way a model run killed the session): putting the
# model in its own unit stops systemd tearing down the agent's scope, but it
# does NOT stop the KERNEL choosing the agent as an additional victim when the
# whole box runs out. On 2026-08-05 a global OOM killed the model unit AND the
# agent session, even though the unit already carried OOMScoreAdjust=1000.
# The asymmetry was only half-built: models were marked as preferred victims,
# nothing marked the session as protected. A negative adj needs
# CAP_SYS_RESOURCE, hence sudo; failure is non-fatal (best-effort shielding
# must never block a run from starting).
for _p in $(pgrep -f 'claude --resume' 2>/dev/null); do
  sudo -n sh -c "echo -700 > /proc/$_p/oom_score_adj" 2>/dev/null
done
echo "  agent session shielded (oom_score_adj=-700); model unit is the preferred victim (+1000)"

# --collect        : auto-reap so a failed unit does not linger in `systemctl --failed`
# OOMScoreAdjust   : if the BOX runs out, the kernel picks this model first --
#                    never dbus, never the agent, never a bystander service.
#                    (2026-07-27 the global OOM killer took dbus and livelocked
#                    the box; 2026-08-04 it took the agent's whole scope.)
# NOTE: MemorySwapMax=0 was tried and removed -- denying swap to a cgroup whose
# mmap'd model is 80 GiB just converts reclaim into an OOM kill.
if (( PROTECT )); then
  # Long, non-resumable job: make it a LAST-resort victim and reserve its
  # working set so the kernel reclaims from the box's other tenants first.
  # need_gib was derived from the actual model file above.
  OOM_ADJ=-800
  MEM_MIN=$(( need_gib > 0 ? need_gib : 40 ))
  EXTRA=( --property=MemoryMin="${MEM_MIN}G" )
  echo "  PROTECTED run: oom_score_adj=$OOM_ADJ, MemoryMin=${MEM_MIN}G"
else
  OOM_ADJ=1000
  EXTRA=()
fi

sudo -n systemd-run \
  --unit="$UNIT" \
  --collect \
  --property=MemoryMax="$MEM" \
  --property=OOMScoreAdjust="$OOM_ADJ" \
  "${EXTRA[@]}" \
  --property=OOMPolicy=stop \
  --property=WorkingDirectory="$PWD" \
  --property=Environment=LD_LIBRARY_PATH=/home/lucas/lib-override \
  --property=StandardOutput="append:$LOGFILE" \
  --property=StandardError="append:$LOGFILE" \
  --uid=lucas \
  "$@" || die "systemd-run failed to start $UNIT"

echo "started. follow with:  $0 --logs $NAME   |   block with: $0 --wait $NAME"
