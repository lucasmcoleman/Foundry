# ROCmFPX (AMD-tuned GGUF quants)

[ROCmFPX](https://github.com/ciru-ai/ROCmFPX) is a llama.cpp fork that adds a
family of GGUF quant types purpose-built for AMD ROCm/RDNA hardware: **ROCmFP3,
ROCmFP4, ROCmFP6, ROCmFP8**, each with a *straight* (size-optimal) and an
*agent* (tool-calling/JSON/coding structure-preserving) preset. Where
MagicQuant searches a per-tensor-group hybrid config, ROCmFPX applies one
uniform, AMD-native quant type across the whole model — a different point on
the size/quality/hardware-fit tradeoff, not a replacement.

This is an optional feature surfaced as Foundry's **ROCmFPX** pipeline stage.
Unlike MagicQuant (a sibling Python package), ROCmFPX is a native C++ project
— it is git-cloned and compiled on first use, not pip-installed.

---

## What the stage does

1. Resolves a source model the same way MagicQuant does: `reap_model >
   heretic_model > merged_model` (safetensors), or an explicit `source_model`
   override, or a cached `model-bf16.gguf`.
2. **Finds or builds ROCmFPX** (`core/_rocmfpx_entry.py:ensure_rocmfpx`):
   probes a few known locations for an existing build exposing the full
   ROCmFP3/6/8 family (a build limited to ROCmFP4 alone — e.g. a plain
   `rocmfp4-llama` checkout — is not accepted), and if none is found, clones
   `ciru-ai/ROCmFPX` pinned to a known commit and runs its Strix Halo
   (`gfx1151`) build script.
3. **Converts to BF16 GGUF** via ROCmFPX's own bundled `convert_hf_to_gguf.py`
   if the source is safetensors and no `model-bf16.gguf` is already cached
   (reused across stages/runs).
4. **Quantizes** with `llama-quantize <bf16.gguf> <out.gguf> <TYPE>` once per
   requested format, where `<TYPE>` comes from a fixed table:

   | format | straight | agent |
   |---|---|---|
   | rocmfp3 | `Q3_0_ROCMFPX` | `Q3_0_ROCMFPX_AGENT` |
   | rocmfp4 | `Q4_0_ROCMFP4` | `Q4_0_ROCMFP4_COHERENT` |
   | rocmfp6 | `Q6_0_ROCMFPX` | `Q6_0_ROCMFPX_AGENT` |
   | rocmfp8 | `Q8_0_ROCMFPX` | `Q8_0_ROCMFPX_AGENT` |

5. Writes `<output>/rocmfpx/<model>-<TYPE>.gguf`.

Default formats: `rocmfp4-agent`, `rocmfp6-agent`, `rocmfp8-agent` — three
tiers (mirroring MagicQuant's Q4/Q5/Q6 convention), agent presets by default
since Foundry's primary use case is agent/tool-calling models.

## MagicQuant-hybrid mode (`mq-<tier>`)

The headline composition: instead of a uniform preset, produce a ROCmFPX GGUF
whose **per-tensor-group precision matches a MagicQuant search**. Add format
specs `mq-q4` / `mq-q5` / `mq-q6` (any tier present in the run's
`magicquant/search_results.json`). For each:

1. Read `tiered[<TIER>]["config"]` — the group→scheme map the evolutionary
   search chose (e.g. `E:BF16 O:Q8_0 X:MXFP4_MOE U:Q4_K_M ...`).
2. Translate each scheme to the nearest ROCmFPX-family type, **rounding up in
   quality** (`Q6_K`/`Q5_K`→`Q6_0_ROCMFPX`, `Q4_K`/`IQ4_NL`/`MXFP4`→
   `Q4_0_ROCMFP4`, `Q3_K`/`Q2_K`→`Q3_0_ROCMFPX`, `Q8_0`→`Q8_0_ROCMFPX`;
   `BF16`/`F16` pass through). ROCmFPX-native scheme names (from a search run
   with `--magicquant-rocmfpx`) map to themselves.
3. Emit a `--tensor-type-file` from MagicQuant's `TensorGroupClassifier`
   patterns, ordered specific-first so llama-quantize's first-match regex
   reproduces the classifier's group assignment, and run `llama-quantize` with
   the tier's highest-quality translated type as the base.

Output: `<model>-ROCMFPX-MQ-<TIER>.gguf`. This is MagicQuant's sensitivity
layout expressed in AMD-native types — "a ROCm-optimized version of a
MagicQuant-optimized quant." Requires the `magicquant` package importable and
the MagicQuant stage to have run first (it now persists `search_results.json`
from both its search paths).

## Native ROCmFPX types inside the MagicQuant search

The tighter integration (see MagicQuant): `--magicquant-rocmfpx` lets the
evolutionary search explore `ROCMFP3/4/6/8` as first-class per-group schemes,
so MagicQuant packs an AMD-native hybrid directly (no translation step). Pair
with `--magicquant-measured` to score candidates by real perplexity instead of
the heuristic predictor. Both are opt-in; the resulting GGUFs load only on the
ROCmFPX fork.

## The CLI

```bash
python core/pipeline.py --model "org/model" \
    --no-export --rocmfpx --rocmfpx-source-model /path/to/merged_or_gguf \
    --rocmfpx-formats rocmfp4-agent rocmfp6-agent rocmfp8-agent \
    --output-dir ./output
```

Flags: `--rocmfpx`/`--no-rocmfpx` (off by default), `--rocmfpx-formats`
(accepts both presets and `mq-<tier>` specs),
`--rocmfpx-source-model`, `--rocmfpx-hint` (point at an existing build instead
of auto-installing). MagicQuant-side: `--magicquant-measured`
(+`--magicquant-rounds`), `--magicquant-rocmfpx`.

```bash
# ROCm-optimized version of a MagicQuant-optimized quant:
python core/pipeline.py --model "org/model" --no-export \
    --rocmfpx --rocmfpx-source-model /path/to/merged \
    --rocmfpx-formats mq-q4 mq-q6
# (MagicQuant runs first, writes search_results.json; rocmfpx reads its Q4/Q6.)
```

## The UI

A ROCmFPX stage card sits alongside MagicQuant with the same shape: a source
override (shown only when Export is disabled), a build-path hint, an optional
imatrix path, and a multi-select tag list of formats.

## Honest caveats

- **Experimental research build.** ROCmFPX's own README calls it that;
  results are hardware-, driver-, model-, and prompt-sensitive. Off by default
  for the same reason `qat` is off by default.
- **Strix Halo (gfx1151) only, for now.** The auto-install only wires the
  Strix build script. `rocmfpx_hint` lets you point at an RDNA2/3/4 build if
  you've built one yourself.
- **No upload wiring yet.** `hf_upload.py` doesn't know about `rocmfpx/`
  outputs — this stage produces local GGUFs only. Deliberately out of scope
  for the first cut (see the design spec); adding it is straightforward
  future work if needed.
- **Not byte-for-byte validated against MagicQuant's evolutionary search.**
  ROCmFPX applies one quant type uniformly; it does not compete with
  MagicQuant's per-tensor-group search, it's a different tool for a different
  tradeoff (AMD-native kernels + a hardware-specific build vs. cross-platform
  hybrid GGUF).

## Where it lives

Entirely inside Foundry (no sibling repo, unlike MagicQuant/heretic-llm):
`core/_rocmfpx_entry.py` (discovery/build/convert/quantize),
`core/services.py::ROCmFPXService` (config builder), `core/pipeline.py`
(`ROCmFPXConfig`, `stage_rocmfpx`), `ui/app.py` (`do_rocmfpx`), `ui/index.html`
(stage card). Design spec:
`docs/maestro/specs/2026-07-01-rocmfpx-stage-design.md`.
