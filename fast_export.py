#!/usr/bin/env python3
"""
Fast LoRA merge + export for AMD APU unified memory systems.

Streams through safetensors shards one at a time, applying LoRA deltas
inline, and writes merged shards to disk. Peak memory: ~6 GB (one shard
+ LoRA weights) instead of 80+ GB for full model load.
"""

import gc
import json
import os
import shutil
import time

import torch
from pathlib import Path
from safetensors.torch import load_file, save_file

MODEL_ID = "DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking"
LORA_DIR = "./output-zeroclaw-qwen40b/lora_adapters"
MERGED_DIR = "./output-zeroclaw-qwen40b/merged_model"


def load_lora_weights(lora_dir: str):
    """Load LoRA adapter config and weights."""
    config_path = os.path.join(lora_dir, "adapter_config.json")
    with open(config_path) as f:
        lora_config = json.load(f)

    # Load LoRA weight tensors
    weights_path = os.path.join(lora_dir, "adapter_model.safetensors")
    lora_weights = load_file(weights_path, device="cpu")

    print(f"LoRA config: r={lora_config['r']}, alpha={lora_config['lora_alpha']}")
    print(f"Target modules: {lora_config['target_modules']}")
    print(f"LoRA tensors: {len(lora_weights)}")

    return lora_config, lora_weights


def build_lora_map(lora_config, lora_weights):
    """Build a map from base model weight name -> (lora_A, lora_B, scaling)."""
    r = lora_config["r"]
    alpha = lora_config["lora_alpha"]
    scaling = alpha / r

    lora_map = {}

    # LoRA weight names are like:
    # base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight
    # base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight
    lora_a_keys = {k: v for k, v in lora_weights.items() if ".lora_A." in k}

    for a_key, a_weight in lora_a_keys.items():
        b_key = a_key.replace(".lora_A.", ".lora_B.")
        if b_key not in lora_weights:
            print(f"  WARNING: No lora_B for {a_key}")
            continue

        b_weight = lora_weights[b_key]

        # Convert LoRA key to base model key
        # base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight
        # -> model.layers.0.self_attn.q_proj.weight
        base_key = a_key.replace("base_model.model.", "").replace(".lora_A.weight", ".weight")

        lora_map[base_key] = (a_weight, b_weight, scaling)

    print(f"LoRA merge targets: {len(lora_map)} weight matrices")
    return lora_map


def streaming_merge():
    """Merge LoRA into base model shard-by-shard."""
    from huggingface_hub import snapshot_download

    print(f"Loading LoRA from {LORA_DIR}")
    lora_config, lora_weights = load_lora_weights(LORA_DIR)
    lora_map = build_lora_map(lora_config, lora_weights)

    print(f"\nEnsuring base model cached: {MODEL_ID}")
    model_path = snapshot_download(MODEL_ID)

    # Load safetensors index
    idx_path = os.path.join(model_path, "model.safetensors.index.json")
    with open(idx_path) as f:
        idx = json.load(f)
    weight_map = idx["weight_map"]

    # Group by shard
    shards = {}
    for name, shard in weight_map.items():
        shards.setdefault(shard, []).append(name)

    # Prepare output directory
    merged_path = Path(MERGED_DIR)
    merged_path.mkdir(parents=True, exist_ok=True)

    # Copy config files from base model
    for fname in ["config.json", "generation_config.json", "special_tokens_map.json"]:
        src = os.path.join(model_path, fname)
        if os.path.exists(src):
            shutil.copy2(src, merged_path / fname)

    # Copy tokenizer files from LoRA dir (may have updates)
    for fname in ["tokenizer.json", "tokenizer_config.json", "chat_template.jinja"]:
        src = os.path.join(LORA_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, merged_path / fname)
        else:
            src2 = os.path.join(model_path, fname)
            if os.path.exists(src2):
                shutil.copy2(src2, merged_path / fname)

    merged_count = 0
    total_t0 = time.time()
    new_weight_map = {}

    for shard_idx, (shard_name, tensor_names) in enumerate(sorted(shards.items())):
        shard_path = os.path.join(model_path, shard_name)
        shard_size = os.path.getsize(shard_path) / 1e9

        t0 = time.time()
        print(f"[{shard_idx+1}/{len(shards)}] Processing {shard_name} ({shard_size:.1f} GB, {len(tensor_names)} tensors)...")

        # Load shard
        shard_data = load_file(shard_path, device="cpu")

        # Apply LoRA deltas
        modified = 0
        for name in tensor_names:
            if name in lora_map:
                a_weight, b_weight, scaling = lora_map[name]
                w = shard_data[name].float()
                # LoRA merge: W_new = W + scaling * (B @ A)
                delta = scaling * (b_weight.float() @ a_weight.float())
                shard_data[name] = (w + delta).to(shard_data[name].dtype)
                modified += 1
                merged_count += 1
                del w, delta

            new_weight_map[name] = shard_name

        # Save merged shard
        out_path = merged_path / shard_name
        save_file(shard_data, str(out_path))

        del shard_data
        gc.collect()

        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.1f}s ({modified} LoRA merges)")

    # Write updated index
    new_index = {
        "metadata": idx.get("metadata", {}),
        "weight_map": new_weight_map,
    }
    with open(merged_path / "model.safetensors.index.json", "w") as f:
        json.dump(new_index, f, indent=2)

    total_time = time.time() - total_t0
    print(f"\nMerge complete in {total_time:.0f}s | {merged_count} weights merged | Output: {MERGED_DIR}")


def main():
    streaming_merge()
    print("PIPELINE_STAGE_COMPLETE=export")


if __name__ == "__main__":
    main()
