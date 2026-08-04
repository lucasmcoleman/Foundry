# Our Own ds4 GGUF for DeepSeek-V4-Flash-0731

Date: 2026-08-03. Status: approved design. Split from
`2026-08-03-size-target-quant-design.md` on spec review — this is an
independent workstream (external quantizer, large downloads, one reboot,
per-step human approval) sharing no code with the size-target feature.

## Goal

Produce our own ds4-loadable GGUF of DeepSeek-V4-Flash-0731 and run it under
antirez's ds4 on this box (Strix Halo gfx1151, 121 GiB usable unified RAM).

## Constraints that shape everything (verified 2026-08-03; external facts —
re-verify against the live repos on build day)

- ds4 loads ONLY its own fixed recipes: per-tensor-role types are hardcoded in
  ds4.c and it dies loudly on anything else. MagicQuant output cannot be made
  loadable; the only quantizer that emits ds4's layout is ds4's own
  `gguf-tools/deepseek4-quantize` (plain C, reads official HF safetensors,
  requires a `--template` GGUF as metadata/tokenizer donor, supports
  `--imatrix` with llama.cpp legacy `.dat` files).
- The only recipe family that fits 121 GiB is
  `IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8` (~81 GiB; experts IQ2_XXS/Q2_K,
  everything else Q8_0). The Q4-expert family renders 150–170 GB.
- SSD-streaming for DeepSeek is Metal-only; on ROCm the model must fit in
  GPU-visible memory. STRIXHALO.md's GRUB GTT parameters assume 128 GB boxes
  and must be scaled down for 121 GiB.
- Model: 304B total / ~13B active MoE, 167 GB native FP8-attn/FP4-expert
  checkpoint, MIT license. Disk free: ~1.1 TB (sufficient: 167 GB checkpoint
  + ~85 GB template + 2 × ~81 GiB outputs).
- Known instability: ds4 self-describes as beta. Open ROCm regression —
  garbled DeepSeek output on some `main` builds — is tracked in ds4 issues
  **#364 and #577** (the fix status changes week to week); ds4 issue **#16**
  is the long-running Strix Halo / ROCm discussion thread and is the place to
  check current known-good state. Pin a commit verified against both on build
  day; record the SHA in the runbook.

## Why build our own instead of downloading antirez's prebuilt

The recipe is fixed, so the only differentiator is **calibration**: without an
imatrix, `iq2_xxs` uses a synthetic weight-energy fallback that ds4's own docs
call worse than measured activations. We own a calibration-corpus discipline
(MagicQuant's multilingual + code + math + agentic blend, eval-disjoint) and
can measure whether it beats the prebuilt.

Honest-outcome rule: if our imatrix build does not measure better than
antirez's prebuilt on the same eval, report that and use his.

## Plan

1. **Pin + build.** Clone `antirez/ds4` at the verified commit;
   `make strix-halo`; build `gguf-tools`. Record SHA + build flags.
2. **Download.** `deepseek-ai/DeepSeek-V4-Flash-0731` safetensors (167 GB) +
   antirez's prebuilt Q2 GGUF (serves as both `--template` donor AND the
   comparison baseline AND the imatrix-collection bootstrap).
3. **Two-pass imatrix.**
   a. Collect activations with ds4's imatrix mode (real prefill graph) over
      our calibration corpus, running the prebuilt/bootstrap quant.
   b. Quantize the official checkpoint with `deepseek4-quantize --imatrix`
      → `IQ2XXS-w2Q2K` recipe (~81 GiB output).
   c. (Fallback if the bootstrap route fails: first quantize with synthetic
      importance, collect imatrix on that, requantize.)
4. **System prep — each step presented to Lucas for approval before
   execution.** GRUB GTT aperture scaled for 121 GiB (~105–110 GiB, exact
   values computed and shown with the arithmetic); reboot; LM Studio and
   llama-swap stopped while ds4 runs; earlyoom stays armed. First model load
   is treated as an OOM-risk event (nothing else resident, console access).
5. **Validate.** Sane-output smoke test (the known regression manifests as
   garbled text); short perplexity sanity run; A/B against the prebuilt on
   the same eval text; record numbers in the runbook.
6. **Optional follow-on.** DSpark support GGUF (speculative decoding);
   community-reported ~2x generation speedups are unverified — measure before
   believing.

## Placement

`scripts/` + a tracked runbook doc in Foundry (`docs/ds4-deepseek.md`). NOT a
pipeline stage: model-specific, external quantizer, hardware procedure.
Durable procedure lives in tracked code (the `output/`-gitignore lesson).

## Non-goals

- Making ds4 read MagicQuant/llama.cpp GGUFs (requires forking ds4).
- The Q4-expert recipe family (cannot fit in 121 GiB).
- Multi-machine / tensor-parallel ds4 (Metal-only today).

## Risks

- 121 GiB is below every community-reported working config (128 GB); the GTT
  aperture math has less headroom than any precedent. Mitigation: compute
  conservatively, treat first load as an experiment, keep the GRUB change
  trivially revertible (documented rollback line).
- ds4 `main` churns daily; the pinned SHA can be stale by build day — the
  runbook's first step is always "re-check #16/#364/#577 state."
- Imatrix collection runs the full prefill graph over the corpus on this box
  — expect hours; schedule like a measurement pass (never interrupted to
  "speed it up": unified memory, bandwidth-bound).

## Acceptance

- ds4 serves DeepSeek-V4-Flash-0731 on this box with coherent output.
- Our imatrix build measurably ≥ the prebuilt, or the honest-outcome rule is
  exercised and documented.
- Runbook committed: pinned SHA, GRUB values + rollback, exact commands,
  measured numbers.
