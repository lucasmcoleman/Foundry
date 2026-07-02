# Design: ROCmFPX Pipeline Stage

**Date:** 2026-07-01
**Status:** Approved (user requested implementation directly; unavailable for live review — proceeding on the assumptions below, flagged for correction on return)
**Owner:** Foundry maintainer
**Target hardware:** Strix Halo APU (gfx1151), this box

## Goal

Add a new optional pipeline stage, `rocmfpx`, that produces AMD-tuned GGUF
quantizations of a model using [ciru-ai/ROCmFPX](https://github.com/ciru-ai/ROCmFPX)
— a llama.cpp fork adding the ROCmFP3/4/6/8 quant-type family (each with a
"straight" and an "agent" tool-calling/JSON-safe preset). The stage runs
parallel to `magicquant` (both are terminal quantization stages before
upload); both are independently toggleable.

## What ROCmFPX actually is

Confirmed by fetching the repo (`ciru-ai/ROCmFPX`, MIT, fork of
`charlie12345/ROCmFPX`, pushed 2026-06-30):

- A full llama.cpp source tree (C++), not a Python/pip package. Must be
  cloned and compiled.
- Ships a Strix Halo build script: `scripts/build-strix-rocmfp4-mtp.sh` →
  `build-strix-rocmfp4/bin/{llama-quantize,llama-cli,llama-server,...}`.
- Adds GGUF quant types via `llama-quantize <src> <out> <TYPE>`, where
  `<TYPE>` is one of a fixed FORMAT×PROFILE table (see below).
- Also carries a standard `convert_hf_to_gguf.py` at repo root (it's a full
  llama.cpp checkout), which already registers
  `Qwen3_5MoeForConditionalGeneration`/`Qwen3_5MoeForCausalLM` — i.e. current
  Qwen3.5 MoE/VLM architectures are supported for the safetensors→GGUF step.
- Explicit upstream caveat: "experimental research build... hardware-,
  driver-, model-, and prompt-sensitive." Treated as such here (off by
  default, like `qat`).

This box already has a related, but narrower, local build at
`/server/ai/strix-halo-club/engines/rocmfp4-llama-src` (forked from
`charlie12345/rocmfp4-llama`, same upstream author) — checked and found to
only support the ROCmFP4 family (no ROCmFP3/6/8), so it's used only as an
opportunistic discovery hint, not a substitute for building ciru-ai/ROCmFPX.

## Architecture

### Stage placement

```
training → export → heretic → reap → qat → ┬─ magicquant  (existing, evolutionary hybrid GGUF tiers)
                                            └─ rocmfpx      (new, AMD-tuned straight quant GGUFs)
                                                     ↘ upload
```

- Same source-priority resolution as `magicquant`:
  `reap_model > heretic_model > merged_model` (safetensors), with a cached
  `model-bf16.gguf` reused if already present (existing `Artifacts.bf16_gguf`
  convention).
- Independent toggle (`Optional[ROCmFPXConfig]`), **off by default** — new
  external native dependency, matches the `qat` precedent rather than
  `magicquant`'s on-by-default.
- Skips itself if the marker matches (same 3-part contract as every other
  stage: marker + config hash + non-empty key artifact — first `*.gguf` in
  `rocmfpx/`).

### What the stage does

1. Resolve source (safetensors dir, priority chain above).
2. `ensure_rocmfpx()`: find or auto-install a ROCmFPX build (mirrors
   `_magicquant_entry.ensure_llamacpp` exactly — pinned commit, not a
   floating branch: `ciru-ai/ROCmFPX@221402af8574faf652b101b6afe225a3f329561f`,
   built via `scripts/build-strix-rocmfp4-mtp.sh`).
3. If `{output}/model-bf16.gguf` doesn't already exist, produce it via the
   ROCmFPX checkout's own `convert_hf_to_gguf.py --outtype bf16` (reused by
   later runs/stages, consistent with the existing fallback-source
   convention already in `reap_common.resolve_artifact_source`).
4. For each requested `(format, profile)` pair, invoke `llama-quantize`
   **directly** (not the shell wrapper script, since the wrapper's path
   isn't guaranteed across forks/reused builds — the direct binary call is
   the same one the README documents as an equivalent alternative) with the
   FORMAT/PROFILE → GGML-type mapping below.
5. Write outputs to `{output_dir}/rocmfpx/<model>-<TYPE>.gguf`.

### FORMAT/PROFILE → GGML type table (from ROCmFPX's README, hardcoded)

| format | profile=straight | profile=agent |
|---|---|---|
| rocmfp3 | `Q3_0_ROCMFPX` | `Q3_0_ROCMFPX_AGENT` |
| rocmfp4 | `Q4_0_ROCMFP4` | `Q4_0_ROCMFP4_COHERENT` |
| rocmfp6 | `Q6_0_ROCMFPX` | `Q6_0_ROCMFPX_AGENT` |
| rocmfp8 | `Q8_0_ROCMFPX` | `Q8_0_ROCMFPX_AGENT` |

Default requested formats: `["rocmfp4-agent", "rocmfp6-agent", "rocmfp8-agent"]`
— three tiers (mirrors MagicQuant's Q4/Q5/Q6 convention), agent presets by
default since this pipeline's primary use case (per CLAUDE.md) is
agent/tool-calling models.

## Configuration surface

### `ROCmFPXConfig` (dataclass in `core/pipeline.py`)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `formats` | list[str] | `["rocmfp4-agent","rocmfp6-agent","rocmfp8-agent"]` | `"<format>-<profile>"` strings (profile defaults to `straight` if omitted, e.g. `"rocmfp3"`) |
| `source_model` | str | `""` | Override source path for standalone runs (mirrors `MagicQuantConfig`) |
| `rocmfpx_hint` | str | `""` | Path hint for an existing ROCmFPX/llama.cpp-fork build (mirrors `llamacpp_path`) |
| `imatrix` | str | `""` | Optional path to an imatrix GGUF, forwarded to `llama-quantize --imatrix` |

### CLI flags

`--rocmfpx` / `--no-rocmfpx` (off by default), `--rocmfpx-formats` (space
list), `--rocmfpx-source-model`, `--rocmfpx-hint`.

### UI

`ROCmFPXCfg` Pydantic model (same fields), `do_rocmfpx()` mirroring
`do_magicquant()`, stage card + `renderROCmFPX()` panel mirroring
MagicQuant's, registered in `STAGE_RUNNERS`/`ALL_STAGES`/`RunRequest`/
`validate_pipeline` (same "no export → needs source_model or existing
artifacts" dependency check as MagicQuant).

## Auto-install pattern (matches `ensure_llamacpp`)

`core/_rocmfpx_entry.py`:
- `find_rocmfpx(hint)`: probes hint → `ROCMFPX_PATH` env → this box's known
  strix-halo-club build (as a hint only, see caveat above — rejected unless
  it exposes the full FP3/FP6/FP8 family) → `~/ROCmFPX` → `./ROCmFPX`.
- `ensure_rocmfpx(hint)`: on miss, `git clone --depth 1` is insufficient
  here because we need a specific commit (not a tag) — clone full then
  `git checkout <pinned SHA>`, then run the Strix build script with
  `JOBS=<cpu_count>`. Reports clearly and exits non-zero on failure (does
  **not** silently skip, matching the `onnx` stage design precedent).

## Out of scope (YAGNI cuts)

- **HF upload wiring** (`did_rocmfpx`/`upload_rocmfpx` in `hf_upload.py`).
  Not requested; the immediate ask is "build ROCmFPX versions" and "create a
  ...ROCm optimized version" locally, not publish one. Left as clearly-scoped
  future work rather than half-wired now.
- **RDNA2/3/4 build variants.** Only the Strix Halo (`gfx1151`) build script
  is wired, since that's this box's hardware and the only target CLAUDE.md
  documents. `rocmfpx_hint` lets a user point at a different pre-built
  install if they have one.
- **TurboQuant K/V-cache runtime types.** Those are a serving-time flag
  (`-ctk`/`-ctv`), not a model-weight quant Foundry produces at build time —
  out of scope for a build pipeline.
- **`configs/*.yaml` example section.** No existing sample config has a
  `qat:`/`magicquant:` nested section either (all three are flat
  training-only configs) — not introducing the first one for `rocmfpx`
  alone.

## Docs

`Foundry/docs/rocmfpx.md`, mirroring the `qat.md` outline: what it does →
mechanism → auto-install pattern → CLI/UI usage → caveats (upstream's own
"experimental research build" disclaimer) → where it lives. Plus a short
addition to `CLAUDE.md`'s Pipeline Stages section.

## Validation criteria for "stage works"

1. `find_rocmfpx`/`ensure_rocmfpx`/format-mapping logic unit-tested without
   the actual binary (stdlib-only, mirrors `_qat_entry.py`'s import-lazily
   convention).
2. A real build of `ciru-ai/ROCmFPX` succeeds on this box and produces a
   working `llama-quantize` with the full FP3/4/6/8 + agent type family.
3. Running `--rocmfpx` end-to-end on a real source model produces
   `rocmfpx/*.gguf` files loadable by `llama-cli`.
