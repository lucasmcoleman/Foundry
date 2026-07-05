#!/usr/bin/env python3
"""
Fast LoRA merge + export for AMD APU unified memory systems.

WHY THIS EXISTS:
Standard save_pretrained_merged() loads the entire model into memory to merge
LoRA weights — 80+ GB for a 40B model. On the Strix Halo APU this exhausts the
shared 128 GB GTT pool and forces swapping to disk.

This script streams through safetensors shards one at a time, applying LoRA
deltas (W_new = W + scaling * B @ A) inline on GPU, and writes merged shards
to disk. Peak memory: ~6 GB (one shard + LoRA weights in memory at a time).

The output is a standard HuggingFace safetensors model directory that can be
fed directly to MagicQuant for GGUF quantization or loaded by any HF-compatible
tool.
"""

import gc
import json
import os
import re
import shutil
import time

import torch
from pathlib import Path
from safetensors.torch import load_file, save_file


def get_device() -> torch.device:
    """Detect the best available device at runtime.

    Uses GPU for LoRA merge math (matmul B @ A). On unified memory this is
    effectively free since the tensors are in the same physical RAM, but the
    GPU can parallelize the matmul across all compute units.
    """
    if torch.cuda.is_available():
        return torch.device("cuda:0")
    return torch.device("cpu")


DEVICE = get_device()


# ── GGUF source detection / pass-through ────────────────────────────────────
#
# Quantization-only runs often start from a GGUF (a local BF16 file or a HF
# repo that publishes a whole ladder of quants). Those must NOT go through the
# safetensors merge below — and a GGUF repo must not be snapshot-downloaded
# wholesale (a real case pulled 245 GB / 15 quant files when only the 54.7 GB
# BF16 was needed). MagicQuant/ROCmFPX refuse pre-quantized inputs, so only
# BF16/F16/F32 GGUFs qualify as sources.

_GGUF_PART_RE = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
# Highest float precision first; (?<!b)f16 so "f16" doesn't match "bf16".
_GGUF_PRECISION_PATTERNS = (r"bf16", r"(?<!b)f16", r"f32", r"fp32")


def pick_best_gguf(filenames):
    """From a file listing, pick the GGUF(s) usable as a quantization source.

    Chooses the highest-precision float GGUF (BF16 > F16 > F32) and returns
    all parts of it (split GGUFs) sorted, primary part first. Returns [] when
    the listing has no .gguf at all; raises ValueError when it has ONLY
    quantized GGUFs (nothing MagicQuant/ROCmFPX can use as a source).
    """
    ggufs = [f for f in filenames if f.lower().endswith(".gguf")]
    if not ggufs:
        return []
    candidates = [f for f in ggufs if "mmproj" not in Path(f).name.lower()]
    for pattern in _GGUF_PRECISION_PATTERNS:
        matches = [f for f in candidates if re.search(pattern, Path(f).name.lower())]
        if not matches:
            continue
        primary = sorted(matches)[0]
        m = _GGUF_PART_RE.search(primary)
        if m:
            stem = primary[: m.start()]
            return sorted(f for f in candidates if f.startswith(stem))
        return [primary]
    raise ValueError(
        "GGUF-only source has no BF16/F16/F32 file to quantize from "
        "(MagicQuant/ROCmFPX refuse pre-quantized inputs). Available: "
        + ", ".join(sorted(Path(f).name for f in ggufs))
    )


def detect_gguf_source(model_id: str):
    """Return the GGUF file list for ``model_id`` if it is a GGUF source.

    Pure detection — no downloads. Returns a list of repo-relative filenames
    (or a one-element absolute path for a local .gguf file), or None when the
    source is safetensors-shaped and should take the normal merge path.
    """
    p = Path(model_id)
    if p.is_file():
        return [str(p)] if model_id.lower().endswith(".gguf") else None
    if p.is_dir():
        if any(p.glob("*.safetensors")):
            return None
        return pick_best_gguf(sorted(str(f) for f in p.iterdir() if f.is_file())) or None
    if p.exists() or "/" not in model_id:
        return None
    from huggingface_hub import list_repo_files

    try:
        files = list_repo_files(model_id)
    except Exception:
        return None  # unreachable repo: let the normal path raise its own error
    if any(f.lower().endswith(".safetensors") for f in files):
        return None
    return pick_best_gguf(files) or None


def resolve_gguf_source(model_id: str):
    """Materialize a GGUF source locally, downloading ONLY the needed file(s).

    Returns the local path of the (primary) GGUF, or None when ``model_id``
    is not a GGUF source. Split GGUFs are rejected for now: downstream tools
    resolve sibling parts relative to the given path, which a lone symlink in
    the output dir would break — merge them first with llama-gguf-split.
    """
    picked = detect_gguf_source(model_id)
    if picked is None:
        return None
    if len(picked) > 1:
        raise RuntimeError(
            f"GGUF source {model_id} is split into {len(picked)} parts; "
            "pass-through doesn't support split GGUFs yet. Merge them first: "
            f"llama-gguf-split --merge {Path(picked[0]).name} <out.gguf>"
        )
    path = Path(picked[0])
    if path.is_absolute():
        return str(path)
    from huggingface_hub import hf_hub_download

    print(f"GGUF repo detected — downloading only {picked[0]}", flush=True)
    return hf_hub_download(model_id, picked[0])


def load_lora_weights(lora_dir: str):
    """Load LoRA adapter config and weights.

    LoRA adapters are small (~100 MB for r=32 on a 40B model) so we load them
    all into GPU memory upfront. They stay resident while we stream through
    the base model shards.
    """
    config_path = os.path.join(lora_dir, "adapter_config.json")
    with open(config_path) as f:
        lora_config = json.load(f)

    # PEFT can save adapter weights as either safetensors or .bin (PyTorch).
    safetensors_path = os.path.join(lora_dir, "adapter_model.safetensors")
    bin_path = os.path.join(lora_dir, "adapter_model.bin")
    # Load directly to GPU — LoRA weights are small and we need them for
    # every shard that contains a target module.
    if os.path.exists(safetensors_path):
        lora_weights = load_file(safetensors_path, device=str(DEVICE))
    elif os.path.exists(bin_path):
        lora_weights = torch.load(bin_path, map_location=DEVICE, weights_only=True)
    else:
        raise FileNotFoundError(f"No adapter weights found in {lora_dir}")

    print(f"LoRA config: r={lora_config['r']}, alpha={lora_config['lora_alpha']}")
    print(f"Target modules: {lora_config['target_modules']}")
    print(f"LoRA tensors: {len(lora_weights)}")

    return lora_config, lora_weights


def build_lora_map(lora_config, lora_weights):
    """Build a map from base model weight name -> (lora_A, lora_B, scaling).

    LoRA decomposes weight updates as: delta_W = (alpha/r) * B @ A
    where A is (r, in_features) and B is (out_features, r).
    The scaling factor alpha/r controls the magnitude of the update.
    """
    r = lora_config["r"]
    alpha = lora_config["lora_alpha"]
    scaling = alpha / r

    lora_map = {}

    # PEFT names LoRA weights like:
    #   base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight
    #   base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight
    # We pair them and map back to the base model weight name.
    lora_a_keys = {k: v for k, v in lora_weights.items() if ".lora_A." in k}

    for a_key, a_weight in lora_a_keys.items():
        b_key = a_key.replace(".lora_A.", ".lora_B.")
        if b_key not in lora_weights:
            print(f"  WARNING: No lora_B for {a_key}")
            continue

        b_weight = lora_weights[b_key]

        # Strip PEFT prefix and LoRA suffix to get the base model key:
        # "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight"
        #  -> "model.layers.0.self_attn.q_proj.weight"
        base_key = a_key.replace("base_model.model.", "").replace(".lora_A.weight", ".weight")

        lora_map[base_key] = (a_weight, b_weight, scaling)

    print(f"LoRA merge targets: {len(lora_map)} weight matrices")
    return lora_map


def streaming_merge(
    model_id: str = "Tesslate/OmniCoder-9B",
    lora_dir: str = "./output/lora_adapters",
    merged_dir: str = "./output/merged_model",
) -> None:
    """Merge LoRA adapters into base model shard-by-shard.

    For each shard:
    1. Load the shard from disk (buffered I/O)
    2. For each weight that has a LoRA adapter, compute delta on GPU:
       W_new = W + (alpha/r) * B @ A
    3. Save the merged shard to the output directory
    4. Free the shard before loading the next

    This keeps peak memory to ~6 GB (one shard + LoRA weights).

    If lora_dir is None, copies the base model shards without merging (useful
    when exporting a base model without LoRA adapters).
    """
    from huggingface_hub import snapshot_download

    gguf_src = resolve_gguf_source(model_id)
    if gguf_src is not None:
        if lora_dir is not None:
            raise RuntimeError(
                "LoRA adapters can only be merged into safetensors weights, "
                f"but the source is a GGUF ({gguf_src}). Point --model at the "
                "original safetensors repo, or drop the adapters."
            )
        # Pass-through: no merge to do. Link the GGUF where downstream stages
        # (MagicQuant/ROCmFPX resolve_source) already look for it. The name is
        # a convention — the file may be F16/F32 if the repo has no BF16.
        out_root = Path(merged_dir).parent
        out_root.mkdir(parents=True, exist_ok=True)
        link = out_root / "model-bf16.gguf"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(gguf_src, link)
        print(f"GGUF source detected — nothing to merge. Linked {gguf_src} "
              f"-> {link} for the quantization stages.", flush=True)
        return

    if lora_dir is not None:
        print(f"Loading LoRA from {lora_dir}")
        lora_config, lora_weights = load_lora_weights(lora_dir)
        lora_map = build_lora_map(lora_config, lora_weights)
    else:
        print("No LoRA adapters — copying base model shards directly")
        lora_map = {}

    print(f"\nEnsuring base model cached: {model_id}")
    # Safetensors repos sometimes also publish GGUF quants — don't pull those.
    model_path = snapshot_download(model_id, ignore_patterns=["*.gguf"])

    # Multi-shard models have an index.json; single-shard models have a single
    # model.safetensors file with no index.
    idx_path = os.path.join(model_path, "model.safetensors.index.json")
    single_path = os.path.join(model_path, "model.safetensors")
    idx_metadata = {}
    if os.path.exists(idx_path):
        with open(idx_path) as f:
            idx = json.load(f)
        weight_map = idx["weight_map"]
        idx_metadata = idx.get("metadata", {})
    elif os.path.exists(single_path):
        from safetensors import safe_open
        with safe_open(single_path, framework="pt") as f:
            weight_map = {k: "model.safetensors" for k in f.keys()}
    else:
        raise FileNotFoundError(f"No safetensors files found in {model_path}")

    # Group tensors by shard file for sequential processing.
    shards = {}
    for name, shard in weight_map.items():
        shards.setdefault(shard, []).append(name)

    merged_path = Path(merged_dir)
    merged_path.mkdir(parents=True, exist_ok=True)

    # Copy model config files from the base model.
    for fname in ["config.json", "generation_config.json", "special_tokens_map.json"]:
        src = os.path.join(model_path, fname)
        if os.path.exists(src):
            shutil.copy2(src, merged_path / fname)

    # Tokenizer files: prefer LoRA dir (training may have modified tokenizer_config),
    # fall back to base model.
    for fname in ["tokenizer.json", "tokenizer_config.json", "chat_template.jinja"]:
        src = os.path.join(lora_dir, fname) if lora_dir is not None else None
        if src and os.path.exists(src):
            shutil.copy2(src, merged_path / fname)
        else:
            src2 = os.path.join(model_path, fname)
            if os.path.exists(src2):
                shutil.copy2(src2, merged_path / fname)

    # Detect composite naming: LoRA keys use CausalLM names (model.layers.*)
    # but composite model shards use model.language_model.layers.*.
    # Build a mapping from shard tensor names to LoRA map keys.
    _composite_prefix = "model.language_model."
    _has_composite_names = any(k.startswith(_composite_prefix) for k in weight_map)
    if _has_composite_names and lora_map:
        print("  Detected composite model naming — will remap LoRA keys during merge")

    merged_count = 0
    total_t0 = time.time()
    new_weight_map = {}

    for shard_idx, (shard_name, tensor_names) in enumerate(sorted(shards.items())):
        shard_path = os.path.join(model_path, shard_name)
        shard_size = os.path.getsize(shard_path) / 1e9

        t0 = time.time()
        print(f"[{shard_idx+1}/{len(shards)}] Processing {shard_name} ({shard_size:.1f} GB, {len(tensor_names)} tensors)...")

        shard_data = load_file(shard_path, device="cpu")

        try:
            modified = 0
            for name in tensor_names:
                # For composite models, strip the language_model prefix to match
                # LoRA keys (PEFT stores keys without the composite wrapper).
                lora_key = name
                if _has_composite_names and name.startswith(_composite_prefix):
                    lora_key = "model." + name[len(_composite_prefix):]

                if lora_key in lora_map:
                    a_weight, b_weight, scaling = lora_map[lora_key]
                    orig_dtype = shard_data[name].dtype

                    # Perform the LoRA merge on GPU: W_new = W + scaling * (B @ A)
                    # The matmul B @ A is the expensive part and benefits from GPU
                    # parallelism. On unified memory the transfer cost is near zero.
                    w = shard_data[name].to(DEVICE, dtype=torch.float32)
                    delta = scaling * (b_weight.float() @ a_weight.float())
                    shard_data[name] = (w + delta).to(dtype=orig_dtype).cpu()
                    modified += 1
                    merged_count += 1
                    del w, delta

                new_weight_map[name] = shard_name

            # Save merged shard to output directory. Write to a temp file first
            # then atomically rename, so a crash mid-write can't leave a corrupt shard.
            out_path = merged_path / shard_name
            tmp_path = out_path.with_suffix(".tmp")
            save_file(shard_data, str(tmp_path))
            tmp_path.rename(out_path)
        finally:
            # Free shard memory before loading the next one.
            del shard_data
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.1f}s ({modified} LoRA merges)")

    # Write the safetensors index so HF tooling can find the shards.
    new_index = {
        "metadata": idx_metadata,
        "weight_map": new_weight_map,
    }
    with open(merged_path / "model.safetensors.index.json", "w") as f:
        json.dump(new_index, f, indent=2)

    total_time = time.time() - total_t0
    print(f"\nMerge complete in {total_time:.0f}s | {merged_count} weights merged | Output: {merged_dir}")


def main():
    streaming_merge()
    print("PIPELINE_STAGE_COMPLETE=export")


if __name__ == "__main__":
    main()
