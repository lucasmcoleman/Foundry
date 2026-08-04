# ds4 commit pin — decided 2026-08-03 ~23:10
PINNED: b7e9f00 (HEAD at clone time, "Version DeepSeek Flash fixtures by checkpoint", Aug 3)

Why: #577 (ROCm garbled output on DeepSeek V4 Flash, batched-prefill path) has a
minimal targeted fix 45a9dcb "Fix ROCm batched HC norm fusion" (Jul 28), parent
= efdadd4 (the literal commenter-reported-bad SHA). Fix is an ancestor of HEAD;
rocm/ds4_rocm_hc_output_launch.cuh untouched since. PR #617's GLM crash is
scope-limited to GLM paths. #364 is an unrelated KV-cache/prompt-metadata issue.

Status: fixed by inspection, NOT field-verified on Strix Halo since 7-28 (thread
silent since 7-25). MANDATORY before trusting: garbled-output smoke test that
exercises BATCHED prefill (chunk size > 1) — single-token decode was never broken.
Fallback if it garbles: bisect from 11da8b3/45a9dcb; do NOT roll back past
45a9dcb (everything older is commenter-confirmed bad); last-known-good legacy
build was 519c4d8 with the env workaround DS4_METAL_DISABLE_BATCH_HC_NORM_FUSION=1
(~6x slower — emergency only).

## Build (2026-08-03 23:20)
BUILT OK at b7e9f00: ds4, ds4-server, ds4-bench (gfx1151).
Toolchain gaps filled (all AMD repo, ROCm 7.2.2 / 70202, 0 removals, torch verified OK after each):
  hipcc, hip-dev, hip-runtime-amd, libhipblas-dev, libhipblaslt-dev, hipcub-dev, rocprim-dev, rocwmma-dev, hipblas-dev
BUILD REQUIRES: LD_LIBRARY_PATH=/home/lucas/lib-override
  (ROCm's bundled lld needs libxml2.so.2; Ubuntu 26.04 ships .so.16. Lucas's
   pre-existing override dir symlinks .so.16 under the old soname — created
   2026-06-19, same day as the libxml2 upgrade. Scoped to the build only.)
NO GRUB/REBOOT NEEDED: GTT already 112 GiB (ttm pages_limit pre-raised), VRAM 2 GiB.
  80.76 GiB model fits. llama-swap holds ~21 GiB with ttl=300s (self-unloads).

## Status 2026-08-04 (work stopped here, deliberately)

Stopped at Lucas's call: antirez's prebuilt is good enough; compute better spent
elsewhere. What remains on disk (`output/ds4-deepseek/`, gitignored):
  - `prebuilt/` (87 GiB) — antirez's IQ2XXS-w2Q2K imatrix build + DSpark support
    + his imatrix .dat. This is the WORKING model.
  - `ds4/` — engine built for gfx1151, WITH rocm-imatrix.patch applied.
    Rebuild: `LD_LIBRARY_PATH=/home/lucas/lib-override make strix-halo`
Deleted (re-downloadable / regenerable): the 156 GiB official checkpoint
(deepseek-ai/DeepSeek-V4-Flash-0731, public + ungated, 48 shards), a partial
quantize output, and the calibration corpora.

### Measured on this box (prebuilt Q2, 80.76 GiB resident)
  prefill 152 t/s, generation 6.91 t/s, model load ~24-55s, total 81.29 GiB.
  Coherent output — first field confirmation the #577 ROCm regression fix works
  on Strix Halo since that thread went quiet 2026-07-25.

### The unfinished thread (see rocm-imatrix.patch)
Patch enables imatrix collection on ROCm (upstream gates it Metal-only via a
stale guard). Collection RUNS and gate/up statistics match antirez's Metal-
collected reference closely (mean 5.19e-2 vs 5.45e-2, max 8.55 vs 8.29).
**UNRESOLVED:** ffn_down_exps diverges ~450x (ours mean 3.48 vs his 7.73e-3).
Sample size does not explain a mean-scale shift. Do NOT use a ROCm-collected
imatrix until that is understood. Discriminating test: collect ~10x more tokens
— if the down mean falls proportionally it is count normalization; if it holds
at ~3.5 it is a real scale error in the ROCm path.

### If this resumes: the better idea
ds4's loader hardcodes types per tensor role, so MagicQuant cannot drive it
generally. BUT `deepseek4-quantize --tensor-type PFX=TYPE` (repeatable) is the
same interface shape MagicQuant already drives for ROCmFPX, and the routed
experts (256 x 43 layers, ~90% of bytes) accept iq2_xxs/q2_k/q4_k/q8_K.
antirez's recipe is uniform across layers; a MagicQuant per-layer search under a
size budget is the natural experiment. Needs the size-target feature finished
plus a v2 "ds4 profile" (cf. the existing `--target-profile q4nx`).
