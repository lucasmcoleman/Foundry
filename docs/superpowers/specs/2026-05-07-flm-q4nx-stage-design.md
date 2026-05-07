# Design: FLM/Q4NX Pipeline Stage

**Date:** 2026-05-07
**Status:** Approved (awaiting implementation plan)
**Owner:** Foundry maintainer
**Target deployment runtime:** FastFlowLM (FLM) on Linux (Strix Halo APU + XDNA2 NPU)

## Goal

Add a new optional pipeline stage `flm` that produces `.q4nx`-format models suitable for **NPU-only inference via FastFlowLM** on Linux. The stage runs in parallel position to the existing `magicquant` (GGUF) and `onnx` (Quark/ORT-GenAI) stages — all three are independently toggleable so the user can produce GGUF, ONNX, FLM/Q4NX, or any combination per run.

The deployment target is FLM on the same Strix Halo box. FLM uses its own runtime stack: `flm` CLI → `libxrt2`/`libxrt-npu2` → `amdxdna` kernel driver → XDNA2 silicon. Models are `.q4nx` files paired with pre-compiled XCLBIN bitstreams (shipped per-architecture inside the FLM `.deb`). Foundry's job is to produce the `.q4nx` weight file from a fine-tuned model and upload it to HuggingFace; the user manually places it in `~/.config/flm/models/` and registers it in FLM's `model_list.json`.

## Context: why this is separate from the ONNX stage

The two stages target genuinely different runtimes:

- **ONNX stage** (existing, currently on `feat/quark-onnx-stage`): produces an OGA-format ONNX directory consumed by Lemonade's `ryzenai-llm` recipe + VAI EP for **NPU+iGPU Hybrid execution on Windows**. Does not run on Linux today.
- **FLM stage** (this spec): produces a `.q4nx` weight file consumed by FLM's CLI for **NPU-only execution on Linux**. The user's actual deployment.

Both have legitimate use cases (Linux Strix users vs. Windows Strix users sharing fine-tuned models on HF). They live as separate sibling stages because the toolchains, runtimes, and output formats share nothing.

## Non-goals

- **Hybrid NPU+iGPU execution on Linux.** Doesn't exist as a runtime today. Not a Foundry build problem.
- **Automatic registration with FLM's `model_list.json`.** That file lives in FLM's install directory (root-owned in some setups) and editing it is a sysadmin action. The model card will document the manual step.
- **Producing XCLBIN kernels.** FLM ships pre-compiled bitstreams per architecture family. New architectures require new XCLBINs from FastFlowLM.
- **Architectures FLM_Q4NX_Converter doesn't support.** The converter explicitly lists supported families: Gemma 3/4, GPT-OSS, LFM 2, Llama, Phi-4, Qwen 2/2.5/3/3.5, Qwen 2/3 VL. Models outside this list will fail at conversion time with an actionable error.
- **Vision/audio model support beyond what the converter handles.** The converter has `-t vision` (Qwen-VL etc.) and `-t audio` (Gemma 4 only). Foundry's training pipeline is text-only, so the default is `-t language`. The other modes are exposed as config options for users who train multimodal models, but Foundry doesn't actively test those paths.

## Architecture

### Stage placement

```
training → export → heretic → reap → ┬─ magicquant  (existing, → GGUF tiers for llama.cpp / Vulkan / ROCm)
                                     ├─ onnx         (existing, → ONNX/OGA for Windows hybrid)
                                     └─ flm          (NEW, → .q4nx for Linux NPU via FastFlowLM)
                                              ↘ upload (handles all three artifact directories)
```

Each is independent. Toggling any combination on/off mirrors the existing `Optional[*Config]` pattern.

### What the stage does

Two subprocess steps:

**Step 1 — Produce a Q8_0 GGUF** via `llama.cpp/convert_hf_to_gguf.py`. The converter accepts Q4_0/Q4_1/Q4_K_M/Q8_0/Q5_K_M and dequantizes non-target formats to FP32 before re-quantizing. We choose Q8_0 because:
- It's the highest-fidelity input the converter accepts.
- Dequant from Q8_0 → FP32 introduces minimal error vs. dequanting from a more aggressive Q4 tier.
- The README notes: "Dequantizing from a lossy format and re-quantizing introduces additional quantization error compared to converting directly from full-precision weights." Starting from Q8_0 minimizes this.

A user concerned about quant error can override `intermediate_quant` to a different format if their architecture has known preferences (e.g. `lfm2→Q4_0`).

**Step 2 — Run `FLM_Q4NX_Converter/convert.py`** with the Q8_0 GGUF as input. Architecture is auto-detected from GGUF metadata; `-f/--force` available as `force_arch` config override.

### Output directory layout

```
{output_dir}/flm_input.gguf           ← intermediate (Q8_0 GGUF, deleted by default after build)
{output_dir}/flm_model/
├── model.q4nx                        ← Q4NX language weights (always present)
└── vision_weights.q4nx               ← only for vision-capable models like Qwen-VL
```

For audio (Gemma 4 only): the converter writes audio-specific weight files; exact names depend on the model. We don't enforce a specific name in the post-condition check — we verify that `flm_model/` is non-empty after the converter exits 0.

### Source priority

Same chain as `magicquant`/`onnx`: `reap_model > heretic_model > merged_model > config.flm.source_model override`. If the user explicitly points `source_model` at an existing GGUF file (i.e. their own pre-quantized input), the stage skips Step 1 and goes straight to Step 2 with that file.

## Configuration surface

### `FlmConfig` (dataclass in `core/pipeline.py`)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `weight_type` | str | `"language"` | Converter `-t` flag: `language` / `vision` / `audio` |
| `force_arch` | str | `""` | Override GGUF metadata arch detection (e.g. `qwen3`, `llama`); empty = auto |
| `intermediate_quant` | str | `"q8_0"` | llama.cpp output type for Step 1; supported: `q8_0`, `q4_0`, `q4_1`, `q4_k_m`, `q5_k_m` |
| `cleanup_intermediates` | bool | `True` | Delete `flm_input.gguf` after a successful Q4NX build |
| `source_model` | str | `""` | Override source path; can be a safetensors directory OR an existing GGUF file (the latter skips Step 1) |

### `FlmCfg` (Pydantic model in `ui/app.py`)

Same fields as `FlmConfig`. Mirrors the `MagicQuantCfg`/`OnnxCfg` pattern.

### CLI flags (added to `core/pipeline.py`'s argparse)

- `--flm` / `--no-flm` — enable / disable the stage
- `--flm-type {language,vision,audio}`
- `--flm-force-arch <name>`
- `--flm-source-quant {q8_0,q4_0,q4_1,q4_k_m,q5_k_m}` — wires to `intermediate_quant`
- `--no-flm-cleanup`

### `UploadConfig` additions

- `upload_flm: bool = True` — upload `flm_model/` to the HF repo alongside other artifacts.

The HF repo layout becomes (additions in **bold**):

```
<user>/<repo>/
├── *.gguf              ← MagicQuant tiers (existing)
├── onnx_model/         ← ONNX/OGA (existing on its branch)
├── flm_model/          ← .q4nx (new)
└── (optional) lora_adapters/, merged/    ← existing flags
```

### Model card additions (`hf_upload.py`)

Add `did_flm: bool` to `HFUploadConfig`. When true, the model card gets a section like:

> ### FLM / Q4NX (Linux NPU via FastFlowLM)
>
> Built with [FLM_Q4NX_Converter](https://github.com/FastFlowLM/FLM_Q4NX_Converter) for **NPU-only execution** on Strix Halo via FastFlowLM.
>
> FLM doesn't auto-pull from HuggingFace — install manually:
>
> ```bash
> mkdir -p ~/.config/flm/models/<short-name>
> wget https://huggingface.co/<repo>/resolve/main/flm_model/model.q4nx \
>      -P ~/.config/flm/models/<short-name>/
> # If a vision_weights.q4nx is present, fetch that too.
> # Edit FLM's model_list.json (e.g. /opt/fastflowlm/share/flm/model_list.json)
> # to register: see https://github.com/FastFlowLM/FLM_Q4NX_Converter#registering-with-flm
> flm run <short-name>
> ```

The exact `model_list.json` path varies by FLM install; we link the converter README rather than hardcode a path.

## File-level changes

| File | Status | Responsibility |
|---|---|---|
| `core/pipeline.py` | modify | `FlmConfig`, `Artifacts.flm_dir` + `Artifacts.flm_input_gguf`, `stage_flm()`, register in `STAGES`, CLI flags. |
| `core/services.py` | modify | `FlmService.build_script()`. |
| `core/flm_q4nx.py` | **new** | `ensure_flm_converter()`, `ensure_gguf_py()`, `find_convert_script()`, `run_llamacpp_quantize()`, `run_flm_converter()`, `run_flm_pipeline()`. |
| `core/quark_torch_compat.py` | **new (duplicate from ONNX branch)** | The amd-quark torch nightly shim. Same content as the ONNX branch's version; both branches will produce identical files when they later land on master, so git sees no conflict. |
| `core/hf_upload.py` | modify | Add `upload_flm`, `did_flm`; thread through `UploadService.build_script`; copy `flm_model/` tree on upload; emit FLM section in model card. |
| `ui/app.py` | modify | `FlmCfg`, `do_flm()`, register in `STAGE_RUNNERS`/`ALL_STAGES`/`validate_pipeline()`. |
| `ui/index.html` | modify | New stage panel + sidebar entry mirroring MagicQuant/ONNX. |
| `configs/default.yaml` | modify | Add `flm:` section with documented defaults. |
| `pyproject.toml` | modify | Add `flm` extras: `["amd-quark>=0.11", "gguf>=0.12"]`. |
| `tests/test_flm_q4nx.py` | **new** | Unit tests for argv construction and converter-script lookup. |
| `tests/test_flm_stage.py` | **new** | E2E smoke test on TinyLlama-1.1B. Skipped if `amd-quark` or `gguf` not installed. |
| `CLAUDE.md` | modify | Document new stage and Linux-NPU runtime story. |

## Auto-install pattern (matches existing stages)

When `stage_flm` runs:

1. Probe for `amd_quark`, `gguf`, `transformers` Python imports.
2. Install the compat shim (via `core.quark_torch_compat.install()`) before any `import quark.*` so torch nightly compatibility doesn't break the install probe itself.
3. Pip-install missing packages.
4. Probe for a local llama.cpp checkout (reuses existing `ensure_llamacpp` from `core/pipeline.py`).
5. Probe for `~/flm-q4nx-converter/convert.py`. If missing, `git clone --depth 1 https://github.com/FastFlowLM/FLM_Q4NX_Converter.git ~/flm-q4nx-converter`.

Failures at any step raise `RuntimeError` with an actionable message. No silent skips.

## Compat shim duplication

`core/quark_torch_compat.py` is duplicated identically from the ONNX branch. Both branches need it because both invoke amd-quark (Quark for ONNX, gguf-py + amd-quark for FLM converter). When both branches eventually land on master, git compares the files byte-for-byte — identical content means no merge conflict. The duplication is brief code-on-two-branches, not duplication-on-master.

## Memory considerations

llama.cpp's `convert_hf_to_gguf.py --outtype q8_0` loads tensors lazily but holds peak ~1.5× model FP16 size briefly during quantization. FLM_Q4NX_Converter then reads the GGUF and rewrites it; peak ~1× the GGUF size. So total peak is roughly the size of the input safetensors, which is well-tested at every Strix Halo memory budget Foundry already supports.

This stage is significantly lighter than the ONNX stage (which loads full FP16 for AWQ calibration). 70B models that OOM in the ONNX stage will run fine here.

## Skip / resume behavior

If `{output_dir}/flm_model/model.q4nx` already exists, the stage logs `"FLM model already exists at {path} — skipping"` and returns success. To force a rebuild, delete `flm_model/` (and ideally `flm_input.gguf`) before re-running.

## Architecture support and pre-validation

Before launching the converter subprocess, `stage_flm` reads the source's `config.json` (or the produced GGUF's metadata) and checks the architecture against the converter's `configs/` directory. If unsupported, the stage fails fast with a message like:

> Architecture 'foobar' is not supported by FLM_Q4NX_Converter. Supported: gemma3, gemma4, gpt-oss, lfm2, llama, phi-4, qwen2, qwen3, qwen3vl. Skipping FLM stage.

This keeps the failure mode actionable rather than getting deep into the converter before it raises.

## Open questions / future work (not in scope)

- **Per-architecture intermediate quant table.** The converter README says "lfm2→Q4_0; qwen3vl→Q4_1". Foundry currently uses Q8_0 universally. If a smoke test reveals certain architectures perform poorly with Q8_0 input, we'll codify a per-arch override table. Out of scope for this stage.
- **Auto-registering with FLM's `model_list.json`.** Friction-reducing convenience; out of scope because the file location varies by install and editing it is a sysadmin action.
- **Upload-then-flm-pull workflow.** Some FLM installs may support pulling custom models from arbitrary HF repos via a config. We don't handle this; users follow the manual install in the model card.
- **Audio support beyond Gemma 4.** Whatever the converter supports, we expose. No special handling.

## Validation criteria for "stage works"

1. Running the pipeline with `--flm` against a small Llama-family model produces `flm_model/model.q4nx`.
2. `python convert.py --help` from the cloned converter executes (validates clone + venv compat).
3. Uploading the result to a HF repo produces a directory layout that `wget` can fetch.
4. **The produced `.q4nx` actually loads in `flm run`** on the Linux NPU server. This is the only smoke test that confirms the runtime side works — no automated CI substitute exists.
5. Existing GGUF-only and ONNX-only runs both still succeed; toggling FLM on/off doesn't affect them.
6. Running with no upstream stages but with `source_model` set (either a safetensors dir or an existing GGUF file) works.

## Sources

- [FLM_Q4NX_Converter GitHub](https://github.com/FastFlowLM/FLM_Q4NX_Converter) (Apache-2.0)
- [FastFlowLM Linux runtime stack](https://github.com/FastFlowLM/FastFlowLM)
- [llama.cpp convert_hf_to_gguf.py](https://github.com/ggml-org/llama.cpp)
- User-supplied technical context on the FLM runtime stack (XRT, libxrt-npu2, amdxdna kernel, /dev/accel/accel0)
- Sibling-stage spec: `docs/superpowers/specs/2026-05-07-quark-onnx-stage-design.md`
