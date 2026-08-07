# Remote GPU runner (`scripts/remote-gpu-run.sh`)

Dispatch bandwidth-bound llama.cpp work to a discrete-GPU host instead of
running it on this box.

## When to use it, and when not to

The split is **capacity vs bandwidth**, and it is not the split you would guess
from this project's usual advice.

| Work | Where | Why |
|---|---|---|
| QAT on a 30B+ model | **here** | `magicquant/qat/train.py` loads the base with a plain `from_pretrained(dtype=…)` — no `load_in_4bit`, no `device_map`, no CPU offload. A 35B at bf16 is ~70 GB. A 24 GB card cannot take the job at all; 121 GB of unified memory is exactly the right hardware. |
| Perplexity / bench on a quant that FITS in the card's VRAM | **remote** | Genuinely much faster — see the correction below. |
| Perplexity on a 60 GB+ BF16 baseline | **here** | Would be partial offload on a 24 GB card, which loses the advantage. |
| MagicQuant distortion table | **here** | CPU ggml encode + error measurement; the GPU is idle either way. |
| Quantize / pack (`llama-quantize`, `create_hybrid_gguf`) | **here** | CPU-bound, and the source files live here. |

## Correction to a rule this repo carries

CLAUDE.md states **"GPU offload does NOT speed up perplexity measurement"**,
measured twice. That is true **and Strix-Halo-specific**. It holds because CPU
and iGPU read the same LPDDR5X at the same ~256 GB/s, so `-ngl` only changes
which engine waits on memory.

It does **not** generalise to a discrete card. A 3090's dedicated GDDR6X runs
at roughly 936 GB/s, ~3.7x this box, and a model resident entirely in VRAM is
genuinely much faster there. Do not cite the Strix Halo finding as a reason to
skip the remote host.

## Setup

The runner assumes only ssh. It does **not** require Foundry, MagicQuant, or a
Python environment on the remote — just a CUDA build of llama.cpp.

1. ssh access. Already configured here as `lucas-pc` in `~/.ssh/config`.
2. A CUDA llama.cpp build on that host:
   ```bash
   git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
   cmake -B build -DGGML_CUDA=ON && cmake --build build -j
   ```
   The runner auto-detects `~/llama.cpp/build/bin`, `~/llama.cpp/build`,
   `/opt/llama.cpp/build/bin`, `/usr/local/bin`, or anything on `PATH`.
   Override with `RGPU_LLAMA`.
3. `./scripts/remote-gpu-run.sh probe` — reports ssh, GPU + VRAM, llama.cpp
   location, and cache free space, naming whatever is missing.

## Use

```bash
./scripts/remote-gpu-run.sh probe
./scripts/remote-gpu-run.sh ppl   output/<run>/model-BUDGET-13.5GiB.gguf
./scripts/remote-gpu-run.sh bench output/<run>/model-BUDGET-13.5GiB.gguf
./scripts/remote-gpu-run.sh sh    'nvidia-smi'
```

Env: `RGPU_HOST` (default `lucas-pc`), `RGPU_CACHE` (default `~/gguf-cache`),
`RGPU_LLAMA`, `RGPU_EVAL` (default wikitext-2 test raw).

## Design notes

- **The VRAM gate refuses rather than degrades.** Full offload is the entire
  reason to dispatch remotely; a model that does not fit would run partially on
  the remote CPU and be *slower* than running it here. The runner checks size
  against VRAM × 0.88 (driver + KV + compute buffers) and fails with an
  explanation instead of silently producing a misleading number.
- **Transfers are rsync `--partial --inplace`,** so a repeat run on the same
  quant costs nothing and an interrupted 14 GB copy resumes.
- **Nothing is cached about the remote between calls** — the llama.cpp path is
  re-detected each time, so a host that gains or loses a build does not leave a
  stale path behind.
- **Comparability caveat.** A perplexity number from the remote host is
  comparable to a local one only if the corpus, `-c`, and `--chunks` match. The
  runner pins `-c 512 --chunks 100` and syncs the same eval file to keep model
  cards consistent; override deliberately, not by accident.
