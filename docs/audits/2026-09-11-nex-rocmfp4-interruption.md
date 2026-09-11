# Nex-N2.5-mini ROCmFP4 interruption review

Date: 2026-09-11. Initial review: `codex/rocmfpx-interruption-20260911`.
Full-model follow-up: `codex/qwen-mtp-inventory-20260911`.

## Current outcome

The user-requested 6.9 GiB corrupt `.gguf.incomplete` file was deleted after
checking its size, timestamp and zero header. The audit and interrupted-output
fixes were merged and pushed to both repositories' default branch, `master`:
MagicQuant `bf01f41`, Foundry `38445c3`. The idle Foundry service was restarted
to activate them; authentication and saved configuration remained intact.

The full retry converted all 733 tensors, without another SIGKILL, but native
loading exposed a separate source-conversion bug: missing
`blk.40.attn_norm.weight`. The checkpoint declares 40 main layers plus one MTP
draft layer, yet all 1,026 source tensors contain **no MTP weights**. The
converter had emitted `qwen35moe.block_count=41` and
`qwen35moe.nextn_predict_layers=1` despite having no tensors for that layer.
This explains the later load failure, not the original SIGKILL.

Reconversion with the official `--no-mtp` option produced a correct 40-block
BF16 source. All 733 output tensor names, types, shapes and byte counts were
unchanged. The prior source was retained as `model-bf16.gguf.invalid-mtp`.
The correction record is
`output/Nex-N2.5-mini/_retest_20260911/source-correction.json`.

The second complete UI retry **passed**, from 20:40:41 to 20:44:05 UTC:

- Output: `output/Nex-N2.5-mini/rocmfpx/Nex-N2.5-mini-Q4_0_ROCMFP4.gguf`,
  **23,341,397,600 bytes** (23.3 GB).
- Total stage: **203.7 seconds**; native quantization: **178.305 seconds**.
- Native loading and WikiText perplexity smoke: **6.16**, four chunks,
  context 512, batch 512, microbatch 128.
- Cgroup peak: **67.09 GiB**; minimum sampled host available memory:
  **62.24 GiB**; no `high`, `max`, `oom`, or `oom_kill` events.
- Evidence: `output/Nex-N2.5-mini/_retest_20260911_no_mtp/result.json`,
  its adjacent resource samples, and `_stage_1789159241468373719.log`.

The automatic follow-up checks safetensors headers and their shard index
before selecting `--no-mtp`, refuses partial or ambiguous inventories, and
preserves present MTP weights. Both BF16 entry helpers reject affected caches
without a matching source/artifact receipt. Conversion output is staged and
structurally validated before publication, with rollback if receipt publication
fails. Receipts track local metadata and file identity, not cryptographic weight
content or model quality. The corrected real cache was verified reusable through
both helpers. Final offline validation: **1011 passed, 1 skipped**, plus required
Pyflakes and diff checks.

The native retry used Foundry `38445c3` and a manually corrected source; the
automatic conversion policy was then verified separately against that source
and with regression tests. The retry used converter checkout `0d313da` and
native build 36 (`221402a`); no runtime rebuild was required.

This artifact is the standalone ROCmFP4 preset. It is not a MagicQuant Q4
search result and does not complete the requested six-variant campaign.
Four-chunk perplexity establishes a smoke-test pass, not comprehensive quality,
throughput, or vision support. No model was uploaded during this retest.

The following sections retain the original investigation and its contemporaneous
observations; the outcome above supersedes their pre-retry status.

## What happened

The Foundry UI ran the standalone `rocmfp4` preset against
`nex-agi/Nex-N2.5-mini`. This was not the `mq-q4` layout from a MagicQuant
search. It used `/server/programming/Foundry` on the original branch, rather
than the audit worktree. The first export attempt passed the model's full Hub
URL, which the Hub client rejected. The corrected `namespace/model` ID worked.

The subsequent BF16 conversion completed successfully. The quantizer then
exited with `-9` (SIGKILL) while converting tensor 197 of 733,
`blk.10.ffn_up_exps.weight`. The executable identified itself as build 36,
revision `221402a`; the checkout's newer HEAD does not identify that binary.

Evidence is in
`output/Nex-N2.5-mini/_stage_1789155761.log` in the original checkout.
The BF16 file is 69,376,636,864 bytes with a GGUF v3 header declaring 733
tensors. Its completion timestamp is 2026-09-11 19:48:15 UTC. The failed
quantized file stopped changing at 19:49:15 UTC and contained 7,406,965,888
bytes, with an entirely zero-filled 24-byte header.

## Cause and limits of the evidence

SIGKILL is confirmed; its sender is not established. There is no recorded
architecture/type rejection. Kernel, earlyoom, systemd-oomd and Foundry logs
examined around the failure did not identify an OOM kill or cancellation.
Foundry's service started before the run and had not restarted. Its cgroup
reported zero `high`, `max`, `oom`, and `oom_kill` events, with a 58.19 GiB
peak against a 70 GiB soft limit and an 85 GiB hard limit. These counters do
not support blaming the service's memory cap.

Swap was almost full when investigated, but a later memory sample cannot
establish memory availability at the instant of failure. Do not present OOM
as a proven diagnosis or raise host memory limits based on this evidence.

An isolated native probe extracted the exact stopped tensor and the source
metadata into a temporary GGUF. The same installed quantizer successfully
converted its 536,870,912 BF16 bytes into 150,994,944 `Q4_0_ROCMFP4` bytes
in 6.72 seconds with four threads. The fork's GGUF reader verified the
resulting tensor name, shape and type. This rules out a consistently failing
conversion of that tensor in isolation; it does not reproduce cumulative
memory conditions, the original thread count, or a full-model run.

The native quantizer reserves a zero-filled GGUF header and finalizes it only
after all tensors finish. Foundry previously gave it the final `.gguf` path,
so a killed process left an invalid file discoverable by upload globs.

## Mitigation and review changes

The corrupt file was preserved as
`rocmfpx/Nex-N2.5-mini-Q4_0_ROCMFP4.gguf.incomplete`, after confirming its
recorded size, timestamp and zero header, and that no quantizer was running.
The BF16 source and merged model were retained.

The review fix stages native quantizer output under a temporary filename,
checks the subprocess result and GGUF tables, offsets and minimum payload spans,
and atomically publishes only
successful output. Both standalone presets and MagicQuant tensor-layout
conversion use the safeguard. Failure diagnostics name terminating signals.
These checks detect this failure mode; they do not replace native loading,
perplexity, quality or throughput validation.

## Initial retry recommendations (historical)

- Run the reviewed code explicitly; the existing UI service still runs the
  original checkout. No production service was restarted by this review.
- Reuse the completed float source after checking source provenance. A new
  model download and conversion are unnecessary for this observed failure.
- Record the quantizer PID, elapsed time, memory/cgroup observations and
  contemporaneous host logs during a controlled retry. An exit code alone
  cannot identify an external SIGKILL sender after the fact.
- Serialize large model workloads and check available unified memory. Do
  not disable memory protection or assume reducing thread count fixes this.
- Validate each completed file with the matching ROCmFPX runtime and measure
  its quality and speed before uploading. This review did not produce or
  publish a successful Nex model. Vision support also needs a compatible
  projector; a text GGUF alone does not supply it.

## Initial validation

The full offline suite passed: **986 passed, 1 skipped**. The skip needs a
historical model artifact. Required Pyflakes and `git diff --check` passed.
The initial sandbox run stalled in FastAPI TestClient; that test passed
immediately outside the sandbox. Two existing fake-worker tests depended on
live host memory and were isolated from that unrelated preflight check.
Dedicated production memory-gate tests remain in the suite.

Reproduce from the review worktree with its MagicQuant dependency on
`PYTHONPATH`:

```bash
PYTHONPATH=/server/programming/MagicQuant-review-20260910 \
  /server/programming/Foundry/.venv/bin/python -m pytest tests/ \
  --ignore=tests/test_training_integration.py -m 'not slow and not gpu' -q
/server/programming/MagicQuant/.venv/bin/ruff check --no-cache --select F core/ tests/
```

The native tensor also passed through the reviewed atomic wrapper: its output
replaced the previous file, preserved mode `0664`, left no staging files, and
loaded with the fork's GGUF reader. This was a four-CPU-affinity functional
probe, not a throughput benchmark.

During final validation the live preflight reported approximately 40.1 GiB
available and 60.6 GiB of pinned GPU GTT. The larger MagicQuant campaign
requires the 48 GiB memory gate to pass; it was not bypassed to start that
campaign.
The native probe's output and timing log are under
`/tmp/nex-rocmfp4-tensor-probe/`; it is an incomplete model fixture and is
not suitable for inference or publication.
