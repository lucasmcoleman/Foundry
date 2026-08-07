#!/usr/bin/env bash
# Dispatch GPU-suitable llama.cpp work to the RTX 3090 box (lucas-pc).
#
# WHY THIS EXISTS
# ---------------
# This box (Strix Halo, gfx1151) has 121 GB of UNIFIED memory, which makes it
# the right hardware for anything capacity-bound -- notably QAT on a 35B model,
# which loads ~70 GB at bf16 with no 4-bit or offload path in
# magicquant/qat/train.py. A 24 GB card cannot take that job at all.
#
# But it is the WRONG hardware for bandwidth-bound work that FITS in a discrete
# card's VRAM. CLAUDE.md records "GPU offload does NOT speed up perplexity
# measurement" -- that finding is STRIX-HALO-SPECIFIC and must not be
# generalised: it holds because CPU and iGPU read the same LPDDR5X at the same
# ~256 GB/s, so offload only changes which engine waits. The 3090's dedicated
# GDDR6X is ~936 GB/s, roughly 3.7x, and a model resident entirely in its VRAM
# genuinely runs much faster there.
#
# Rule: capacity-bound stays here; bandwidth-bound-and-it-fits goes there.
#
# THE HOST IS WINDOWS
# -------------------
# lucas-pc runs Windows; its default ssh shell is cmd.exe. Real work happens in
# WSL2 Debian, which sees the 3090 through the Windows driver (nvidia-smi works
# inside WSL; the CUDA *toolkit* is installed in WSL, the *driver* is not).
# Every command is base64-encoded and piped into `bash` inside WSL, because
# cmd.exe mangles quotes, parentheses and redirections in ways that fail
# confusingly (an unquoted `(` produced "'processors)' is not recognized...",
# and a POSIX `for d in ...` produced "d was unexpected at this time").
#
# Usage:
#   remote-gpu-run.sh probe
#   remote-gpu-run.sh ppl   <model.gguf> [extra llama-perplexity args]
#   remote-gpu-run.sh bench <model.gguf> [extra llama-bench args]
#   remote-gpu-run.sh sh    '<bash snippet run inside WSL>'
#
# Env: RGPU_HOST (lucas-pc) RGPU_DISTRO (Debian) RGPU_LLAMA (/opt/llama.cpp/build/bin)
#      RGPU_CACHE (/opt/gguf-cache) RGPU_EVAL (local wikitext-2 test raw)
set -uo pipefail

HOST="${RGPU_HOST:-lucas-pc}"
DISTRO="${RGPU_DISTRO:-Debian}"
LLAMA="${RGPU_LLAMA:-/opt/llama.cpp/build/bin}"
CACHE="${RGPU_CACHE:-/opt/gguf-cache}"
EVAL="${RGPU_EVAL:-/server/ai/wikitext/wikitext-2-raw/wiki.test.raw}"
SAFETY=0.88          # usable fraction of VRAM: driver + KV + compute buffers

die() { echo "remote-gpu-run: $*" >&2; exit 1; }

# Run a bash snippet inside WSL as root. base64 so cmd.exe never sees a
# metacharacter; the noise filter drops the host's ssh PQ-handshake banner.
rw() {
  # LD_LIBRARY_PATH: the Debian CUDA toolkit installs a STUB libcuda.so.1 at
  # /lib/x86_64-linux-gnu that shadows the real WSL driver library at
  # /usr/lib/wsl/lib -- with the stub, ggml_cuda_init reports "no CUDA-capable
  # device" and llama.cpp silently benches on the WSL CPU (observed: 12 t/s tg,
  # SLOWER than the local box, an actively misleading number). Prefix every
  # remote command so the real driver wins.
  local b64; b64=$(printf '%s' "export LD_LIBRARY_PATH=/usr/lib/wsl/lib:\${LD_LIBRARY_PATH:-}; $1" | base64 -w0)
  timeout "${2:-600}" ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" \
    "wsl -d $DISTRO -u root -- bash -c \"echo $b64 | base64 -d | bash\"" 2>&1 \
    | tr -d '\0' | grep -viE "warning:|openssh|vulnerable|post-quantum|^\*\*"
}

cmd_probe() {
  echo "host: $HOST (Windows) -> WSL:$DISTRO"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" 'echo ok' >/dev/null 2>&1 \
    || die "UNREACHABLE over ssh. Check ~/.ssh/config and key authorisation."
  echo "  ssh: OK"
  rw 'echo "  kernel: $(uname -sr)"
      echo "  ram:    $(free -g | awk "/^Mem:/{print \$2\" GiB total, \"\$7\" available\"}")"
      printf "  gpu:    "; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null || echo "NONE (nvidia-smi missing)"
      printf "  nvcc:   "; (nvcc --version 2>/dev/null | tail -1) || echo "MISSING"
      for b in llama-perplexity llama-bench llama-cli; do
        printf "  %-16s %s\n" "$b:" "$([ -x '"$LLAMA"'/$b ] && echo '"$LLAMA"'/$b || echo MISSING)"
      done
      mkdir -p '"$CACHE"'; printf "  cache:  "; df -h '"$CACHE"' | tail -1' 120
}

# Full offload is the ENTIRE reason to dispatch here. A model that does not fit
# would run partly on the remote CPU and be slower than just running it on the
# unified-memory box -- so refuse loudly rather than return a misleading number.
check_fits() {
  local sz_gib vram_mib vram_gib usable
  sz_gib=$(( $(stat -c%s "$1") / 1024 / 1024 / 1024 ))
  vram_mib=$(rw 'nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits' 90 | tr -dc '0-9')
  [[ -n "$vram_mib" ]] || die "could not read remote VRAM; run 'probe' first"
  vram_gib=$(( vram_mib / 1024 ))
  usable=$(awk -v v="$vram_gib" -v s="$SAFETY" 'BEGIN{printf "%d", v*s}')
  echo "  model ${sz_gib} GiB vs ${vram_gib} GiB VRAM (usable ~${usable} GiB)" >&2
  (( sz_gib <= usable )) || die "model does not fit in remote VRAM.
       Partial offload there would be SLOWER than running it locally on the
       unified-memory box. Run this one locally, or pick a smaller quant."
}

# rsync over an ssh transport that lands inside WSL. --partial --inplace makes
# an interrupted multi-GB copy resumable and a repeat run free.
push() {
  local f="$1" base; base=$(basename "$f")
  rw "mkdir -p $CACHE" 60 >/dev/null
  echo "  syncing $base ..." >&2
  rsync -a --partial --inplace --info=progress2 \
    -e "ssh -o BatchMode=yes" --rsync-path="wsl -d $DISTRO -u root -- rsync" \
    "$f" "$HOST:$CACHE/" >&2 || die "rsync of $base failed"
  echo "$CACHE/$base"
}

cmd_ppl() {
  local m="$1"; shift
  [[ -f "$m" ]] || die "no such model: $m"
  [[ -f "$EVAL" ]] || die "no eval corpus at $EVAL (set RGPU_EVAL)"
  check_fits "$m"
  local rm rme; rm=$(push "$m"); rme=$(push "$EVAL")
  # -c 512 --chunks 100 pinned to match the local methodology quoted on model
  # cards; a PPL from here is only comparable if corpus+ctx+chunks all match.
  echo "  running llama-perplexity on the 3090 (full offload) ..." >&2
  rw "$LLAMA/llama-perplexity -m $rm -f $rme -c 512 --chunks 100 -ngl 999 $* 2>&1 | tail -25" 5400
}

cmd_bench() {
  local m="$1"; shift
  [[ -f "$m" ]] || die "no such model: $m"
  check_fits "$m"
  local rm; rm=$(push "$m")
  rw "$LLAMA/llama-bench -m $rm -ngl 999 $* 2>&1 | tail -20" 3600
}

case "${1:-}" in
  probe) cmd_probe ;;
  ppl)   shift; [[ $# -ge 1 ]] || die "usage: ppl <model.gguf> [args]";   m="$1"; shift; cmd_ppl "$m" "$@" ;;
  bench) shift; [[ $# -ge 1 ]] || die "usage: bench <model.gguf> [args]"; m="$1"; shift; cmd_bench "$m" "$@" ;;
  sh)    shift; [[ $# -ge 1 ]] || die "usage: sh '<bash snippet>'"; rw "$1" "${2:-600}" ;;
  *)     sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
