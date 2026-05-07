# Design: Quark/ONNX Pipeline Stage for NPU Hybrid Execution

**Date:** 2026-05-07
**Status:** Approved (awaiting implementation plan)
**Owner:** Foundry maintainer
**Target deployment runtime:** Lemonade Server on Linux (Strix Halo APU, gfx1151 + XDNA2 NPU)

## Goal

Add a new optional pipeline stage that produces ONNX models suitable for **NPU + iGPU Hybrid execution** via the OGA (ONNX Runtime GenAI) backend in Lemonade Server. The stage runs in parallel position to the existing `magicquant` (GGUF) stage. Both are independently toggleable, so the user can produce GGUF, ONNX, or both per run.

The deployment target is Lemonade on Linux. Lemonade routes ONNX models to OGA (NPU+iGPU hybrid) and GGUF models to llama.cpp (ROCm/Vulkan). Foundry's job stops at producing well-formed model directories and uploading them to a HuggingFace repo; `lemonade pull <repo>` on the server pulls everything and serves per-format.

## Non-goals

- **NPU runtime in Foundry.** Foundry is a build-time tool. Inference happens in Lemonade.
- **FastFlowLM packaging.** FFLM consumes its own kernel-pack format that isn't a documented public conversion target as of March 2026. Lemonade will serve `.flm` files if present, but Foundry won't generate them.
- **NPU-only ONNX (`-e cpu` execution provider).** Hybrid (`-e dml`) is the only supported recipe for now. The config field is exposed for future flexibility but `dml` is the only tested value.
- **Re-quantizing models that are already INT4.** Source must be FP16/BF16 safetensors.
- **Memory-efficient quantization for >40B models.** Quark loads the model in FP16 plus collects activation stats. Models that don't fit in unified memory will surface an OOM error; Foundry will not silently fall back. Adding a streaming/shard-by-shard Quark mode (analogous to `fast_train_zeroclaw`) is future work, not in scope here.

## Architecture

### Stage placement in the pipeline

```
training → export → heretic → reap → ┬─ magicquant  (existing, → GGUF tiers)
                                     └─ onnx         (new, → ONNX/OGA hybrid)
                                              ↘ upload
```

The `onnx` stage:
- Has the same source priority as `magicquant`: `reap_model > heretic_model > merged_model > merged GGUF (failover)`.
- Is independent — toggling it on/off mirrors the existing `cfg.magicquant = None` pattern via `Optional[OnnxConfig]`.
- Runs in a fresh subprocess so GPU memory is fully freed afterwards (matches existing stages).
- Skips itself if `{output_dir}/onnx_model/model.onnx` already exists (matches the artifact-existence-skip pattern used by all other stages).

### What the stage does (two sub-steps in one subprocess)

**Step 1 — Quark INT4 AWQ quantization** via the `quark.torch` Python API:

1. Load merged FP16/BF16 safetensors from the source directory.
2. Build a calibration dataloader: 128 samples × 512 tokens by default, sourced from `pileval_for_awq_benchmark` on HuggingFace. The dataset id is configurable; a local `.jsonl` path is also accepted.
3. Run AWQ-aware INT4 weight-only quantization with group size 128 (`uint4_wo_128`).
4. Export the quantized weights as HF-format safetensors to `{output_dir}/quark_safetensors/`.

**Step 2 — ORT-GenAI model builder** (subprocess):

```bash
python -m onnxruntime_genai.models.builder \
    -i {output_dir}/quark_safetensors \
    -o {output_dir}/onnx_model \
    -p int4 \
    -e dml
```

The `-e dml` flag is what Lemonade-on-Linux's OGA backend routes to NPU+iGPU hybrid (despite the historical "DirectML" name, this is the recipe Lemonade picks up for hybrid execution; the runtime layer abstracts the underlying backend).

### Output directory layout

```
{output_dir}/quark_safetensors/    ← intermediate (deleted by default after build; cleanup_intermediates=false to keep for debugging)
{output_dir}/onnx_model/
├── model.onnx                ← ORT-GenAI builder output
├── model.onnx.data           ← INT4 weights blob
├── genai_config.json         ← OGA runtime config consumed by Lemonade
├── tokenizer.json
├── tokenizer_config.json
├── special_tokens_map.json
└── chat_template.jinja       ← copied from source if present
```

This is the standard OGA model layout. Lemonade discovers it by directory and routes to its OGA backend automatically.

## Configuration surface

### `OnnxConfig` (dataclass in `core/pipeline.py`)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `quant_scheme` | str | `"uint4_wo_128"` | Quark scheme; supported group sizes 32 / 64 / 128 |
| `quant_algo` | str | `"awq"` | `"awq"` or `"gptq"` |
| `execution_provider` | str | `"dml"` | OGA recipe; only `"dml"` (hybrid) is tested |
| `data_type` | str | `"float16"` | Activation dtype; alt: `"bfloat16"` |
| `num_calib_data` | int | `128` | AWQ calibration samples |
| `seq_len` | int | `512` | Calibration sequence length |
| `calib_dataset` | str | `"pileval_for_awq_benchmark"` | HF dataset id or local `.jsonl` path |
| `cleanup_intermediates` | bool | `true` | Delete `quark_safetensors/` after a successful ONNX build (set false to keep for debugging) |
| `source_model` | str | `""` | Override source path for runs without upstream stages (mirrors `MagicQuantConfig.source_model`) |

### `OnnxCfg` (Pydantic model in `ui/app.py`)

Same fields as `OnnxConfig`. Mirrors the pattern of `MagicQuantCfg`.

### CLI flags (added to `core/pipeline.py`'s argparse)

- `--onnx` / `--no-onnx` — enable / disable the stage
- `--onnx-quant-scheme {uint4_wo_32,uint4_wo_64,uint4_wo_128}`
- `--onnx-quant-algo {awq,gptq}`
- `--onnx-ep {dml,cpu}`
- `--onnx-num-calib`, `--onnx-seq-len`, `--onnx-calib-dataset`
- `--onnx-cleanup`

### `UploadConfig` additions

- `upload_onnx: bool = True` — upload `onnx_model/` to the HF repo alongside GGUFs.

The HF repo layout becomes:

```
<user>/<repo>/
├── *.gguf              ← MagicQuant tiers (existing)
├── onnx_model/         ← new: ORT-GenAI hybrid OGA model
└── (optional) lora_adapters/, merged/    ← existing flags
```

Single repo, single `lemonade pull` on the server.

### Model card additions (`hf_upload.py`)

Add `did_onnx: bool` to `HFUploadConfig`. When true, the model card gets a section like:

> ### ONNX (INT4 AWQ, OGA hybrid)
> Built with AMD Quark + onnxruntime-genai for **NPU + iGPU Hybrid execution** on Ryzen AI 300 / Strix Halo.
> Serve via Lemonade Server: `lemonade pull <user>/<repo>` then `lemonade run <repo>`.

## File-level changes

| File | Change |
|---|---|
| `core/pipeline.py` | Add `OnnxConfig` dataclass, `Artifacts.onnx_dir` and `Artifacts.quark_dir` properties, `stage_onnx()` function, registration in `STAGES`, CLI flag handling, dry-run support. |
| `core/services.py` | Add `OnnxService` with `build_script()` mirroring `MagicQuantService`. The generated subprocess script stubs out unneeded heavy deps (vllm, lm_eval) the same way `ReapService` does, and uses the `_env_preamble()` helper. |
| `core/onnx_quark.py` *(new)* | Runtime helper module imported by the subprocess script: `ensure_quark_installed()`, `build_calib_dataloader()`, `run_quark_quantization()`, `run_oga_builder()`. Keeps the generated subprocess script small. |
| `core/hf_upload.py` | Add `upload_onnx`, `did_onnx`. Emit the ONNX section in the generated model card. Recurse into `onnx_model/` when uploading. |
| `ui/app.py` | Add `OnnxCfg` Pydantic model, `do_onnx()` async function, register in `STAGE_RUNNERS`, `ALL_STAGES`, `validate_pipeline()` (require source-or-override when run without upstream stages, same as MagicQuant). |
| `ui/index.html` | New stage panel with the same shape as the MagicQuant panel: enable toggle, quant_scheme select, quant_algo select, execution_provider select, calibration knobs. New stage entry in the sidebar. |
| `configs/default.yaml` | Add an `onnx:` section with the documented defaults. |
| `pyproject.toml` | Add an optional extras group: `onnx = ["amd-quark>=0.11", "onnxruntime-genai>=0.6"]`. |
| `tests/test_onnx_stage.py` *(new)* | Smoke test using a tiny model (e.g. TinyLlama-1.1B) — quantize, build ONNX, verify directory structure and that `genai_config.json` is well-formed. Skipped if `amd-quark` is unavailable. |
| `CLAUDE.md` | Add the new stage to the Pipeline Stages section and reference Lemonade as the runtime target. |

## Auto-install pattern (matches `ensure_llamacpp`)

When `stage_onnx` runs:

1. Probe for `quark.torch` and `onnxruntime_genai` imports.
2. If either is missing, run `pip install amd-quark onnxruntime-genai` into the Foundry venv.
3. Log clearly: "Installing AMD Quark + onnxruntime-genai…" → "Installed" or "Install failed".
4. On install failure, exit non-zero. Do **not** silently skip the stage.

## Memory considerations

Strix Halo unified memory: 124 GB (some reserved for OS / other processes; LM Studio etc. should be unloaded).

| Source model size | Quark FP16 footprint | Status |
|---|---|---|
| 8B | ~16 GB | Comfortable |
| 14B | ~28 GB | Comfortable |
| 30B | ~60 GB + ~10 GB activations | Fits with headroom |
| 40B | ~80 GB + ~10 GB activations | Tight but fits |
| 70B+ | ~140 GB+ | Will OOM — surface a clear error and recommend a smaller source |

If 40B+ becomes a frequent target, future work: streaming/shard-by-shard Quark calibration analogous to `fast_train_zeroclaw.py`.

## Skip / resume behavior

Same pattern as every other stage: if `{output_dir}/onnx_model/model.onnx` already exists, the stage logs "ONNX model already exists at {path} — skipping" and returns success. To force a rebuild, delete `onnx_model/` (and ideally `quark_safetensors/`) before re-running.

## Open questions / future work (not in scope)

- **NPU-only ONNX** (`-e cpu`) for prefill+decode entirely on NPU. Field is exposed; runtime not yet validated.
- **Per-layer group size search** (Quark advanced feature) — analogous to MagicQuant's evolutionary search for GGUF. Could yield a hybrid ONNX with per-tensor group-size assignments. Future MagicQuant-for-ONNX project.
- **FastFlowLM `.flm` packaging** when AMD documents a public converter for arbitrary fine-tuned models.
- **Streaming Quark calibration** for 40B+ models that don't fit at FP16.
- **Lemonade-side smoke test** — automate `lemonade pull` + a 1-token generation against the uploaded repo as a CI gate. Out of scope for this stage; would be a separate test harness.

## Validation criteria for "stage works"

1. Running the pipeline with `--onnx` on a small model (e.g. Qwen3-1.7B from the existing test set) produces `onnx_model/model.onnx` and a valid `genai_config.json`.
2. Uploading the result to a HF repo produces a directory layout `lemonade pull` can consume.
3. `lemonade pull <test-repo>` followed by `lemonade run <test-repo>` on the Linux server completes and generates plausible output (manual smoke test for first integration; can later be automated).
4. Existing GGUF-only and GGUF+ONNX runs both succeed; toggling either off does not affect the other.
5. Running with no upstream stages but with `source_model` set in `OnnxCfg` works (matches MagicQuant's standalone mode).

## Sources

- [AMD Quark for ONNX-GenAI (uint4 OGA) tutorial](https://quark.docs.amd.com/latest/supported_accelerators/ryzenai/tutorial_uint4_oga.html)
- [Ryzen AI Hybrid OGA documentation](https://ryzenai.docs.amd.com/en/latest/hybrid_oga.html)
- [Lemonade Server homepage](https://lemonade-server.ai/)
- [Lemonade SDK GitHub](https://github.com/lemonade-sdk/lemonade)
- [Lemonade FAQ — formats & Linux NPU](https://lemonade-server.ai/docs/faq/)
- [AMD article: LLM apps on Ryzen AI through Lemonade](https://www.amd.com/en/developer/resources/technical-articles/unlocking-a-wave-of-llm-apps-on-ryzen-ai-through-lemonade-server.html)
