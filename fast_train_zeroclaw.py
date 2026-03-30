#!/usr/bin/env python3
"""
Fast training script for AMD APU unified memory systems.

Bypasses transformers' slow tensor-by-tensor loading by:
1. Creating model skeleton on meta device (instant)
2. Loading safetensors shards one at a time (fast buffered I/O)
3. Quantizing with BnB on GPU in bulk per-shard (0.01s/tensor)
4. Freeing each shard before loading the next (controls peak memory)

Then trains with PEFT LoRA + TRL SFTTrainer.
"""

import gc
import json
import os
import time

os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

import torch
from pathlib import Path

MODEL_ID = "DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking"
DATASET_PATH = "zeroclaw_training_data.jsonl"
OUTPUT_DIR = "./output-zeroclaw-qwen40b"
HF_REPO = "lmcoleman/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking-Zeroclaw-GGUF"

# LoRA config
LORA_R = 32
LORA_ALPHA = 64
LORA_DROPOUT = 0.05
TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]

# Training config
NUM_EPOCHS = 3
BATCH_SIZE = 1
GRAD_ACCUM = 8
LEARNING_RATE = 2e-4
MAX_SEQ_LENGTH = 4096

DEVICE = torch.device("cuda:0")


def fast_load_quantized_model():
    """Load model with shard-by-shard quantization. Much faster than transformers' default."""
    from transformers import AutoConfig, AutoTokenizer
    from accelerate import init_empty_weights
    from safetensors.torch import load_file
    from huggingface_hub import hf_hub_download, snapshot_download
    from bitsandbytes.functional import quantize_4bit
    from bitsandbytes.nn import Linear4bit, Params4bit
    import bitsandbytes as bnb

    print(f"Loading config for {MODEL_ID}...")
    config = AutoConfig.from_pretrained(MODEL_ID, trust_remote_code=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, trust_remote_code=True)

    # Download all files to cache (should already be cached)
    print("Ensuring model files are cached...")
    model_path = snapshot_download(MODEL_ID)
    print(f"Model path: {model_path}")

    # Load safetensors index
    idx_path = os.path.join(model_path, "model.safetensors.index.json")
    with open(idx_path) as f:
        idx = json.load(f)
    weight_map = idx["weight_map"]

    # Group tensors by shard
    shards = {}
    for tensor_name, shard_name in weight_map.items():
        shards.setdefault(shard_name, []).append(tensor_name)

    # Create model on meta device (no memory used)
    print("Creating model skeleton on meta device...")
    from transformers import AutoModelForCausalLM
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5ForConditionalGeneration
    with init_empty_weights():
        model = Qwen3_5ForConditionalGeneration(config)

    model.eval()

    # BnB 4-bit config
    quant_type = "nf4"
    compute_dtype = torch.bfloat16
    blocksize = 128  # Required for AMD

    # Replace nn.Linear with bnb.nn.Linear4bit for quantizable layers
    print("Replacing Linear layers with Linear4bit...")
    skip_names = {"lm_head"}

    def replace_linear_with_4bit(module, prefix=""):
        for name, child in list(module.named_children()):
            full_name = f"{prefix}.{name}" if prefix else name
            if isinstance(child, torch.nn.Linear) and not any(s in full_name for s in skip_names):
                if not any(s in full_name for s in ["embed", "norm"]):
                    new_layer = bnb.nn.Linear4bit(
                        child.in_features,
                        child.out_features,
                        bias=child.bias is not None,
                        compute_dtype=compute_dtype,
                        quant_type=quant_type,
                        device="meta",
                    )
                    setattr(module, name, new_layer)
            else:
                replace_linear_with_4bit(child, full_name)

    replace_linear_with_4bit(model)

    # Load shard by shard
    total_t0 = time.time()
    loaded = 0
    total = len(weight_map)

    for shard_idx, (shard_name, tensor_names) in enumerate(sorted(shards.items())):
        shard_path = os.path.join(model_path, shard_name)
        shard_size = os.path.getsize(shard_path) / 1e9

        t0 = time.time()
        print(f"[{shard_idx+1}/{len(shards)}] Loading {shard_name} ({shard_size:.1f} GB, {len(tensor_names)} tensors)...")

        # Load entire shard at once (fast buffered read)
        shard_data = load_file(shard_path, device="cpu")

        for name in tensor_names:
            # Navigate to the parameter in the model
            parts = name.split(".")
            target = model
            for part in parts[:-1]:
                target = getattr(target, part)
            attr = parts[-1]

            tensor = shard_data[name]

            if isinstance(target, bnb.nn.Linear4bit) and attr == "weight":
                # Quantize to 4-bit on GPU and assign to Linear4bit
                tensor_gpu = tensor.to(DEVICE, dtype=compute_dtype)
                target.weight = bnb.nn.Params4bit(
                    tensor_gpu.contiguous(),
                    requires_grad=False,
                    quant_type=quant_type,
                    blocksize=blocksize,
                    compress_statistics=True,
                ).to(DEVICE)
                del tensor_gpu
            else:
                # Keep as-is (embeddings, norms, biases, small tensors)
                dtype = compute_dtype if tensor.is_floating_point() else tensor.dtype
                new_param = torch.nn.Parameter(tensor.to(DEVICE, dtype=dtype), requires_grad=False)
                setattr(target, attr, new_param)

            del tensor
            loaded += 1

        # Free shard data before loading next
        del shard_data
        gc.collect()
        torch.cuda.empty_cache()

        elapsed = time.time() - t0
        total_elapsed = time.time() - total_t0
        print(f"  Done in {elapsed:.1f}s | Progress: {loaded}/{total} ({100*loaded/total:.0f}%) | Total: {total_elapsed:.0f}s")

    total_time = time.time() - total_t0
    gpu_mb = torch.cuda.memory_allocated() / 1e6
    print(f"\nModel loaded in {total_time:.0f}s | GPU allocated: {gpu_mb:.0f} MB")

    return model, tokenizer


def main():
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    # ── Stage 1: Fast model loading ──
    model, tokenizer = fast_load_quantized_model()

    import psutil
    rss = psutil.Process(os.getpid()).memory_info().rss / 1e9
    print(f"Process RSS: {rss:.1f} GB")

    # ── Stage 2: Attach LoRA ──
    print("\nAttaching LoRA adapters...")
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

    lora_config = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=TARGET_MODULES,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())
    print(f"Trainable: {trainable:,} / {total_params:,} ({100*trainable/total_params:.2f}%)")

    # ── Stage 3: Load dataset ──
    print(f"\nLoading dataset: {DATASET_PATH}")
    from datasets import load_dataset

    dataset = load_dataset("json", data_files=DATASET_PATH, split="train")
    print(f"Dataset: {len(dataset)} examples")

    def fmt(ex):
        ex["text"] = tokenizer.apply_chat_template(
            ex["messages"], tokenize=False, add_generation_prompt=False,
        )
        return ex

    dataset = dataset.map(fmt)

    # ── Stage 4: Train ──
    from trl import SFTTrainer, SFTConfig

    training_args = SFTConfig(
        output_dir=OUTPUT_DIR,
        num_train_epochs=NUM_EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUM,
        learning_rate=LEARNING_RATE,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        optim="adamw_8bit",
        weight_decay=0.01,
        max_grad_norm=1.0,
        fp16=False,
        bf16=True,
        logging_steps=1,
        save_strategy="epoch",
        seed=42,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to="none",
        dataset_text_field="text",
    )
    tokenizer.model_max_length = MAX_SEQ_LENGTH

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=training_args,
    )

    print("\nStarting training...")
    stats = trainer.train()
    print(f"\nTraining complete! Final loss: {stats.training_loss:.4f}")

    # ── Stage 5: Save ──
    lora_dir = os.path.join(OUTPUT_DIR, "lora_adapters")
    print(f"\nSaving LoRA adapters to {lora_dir}")
    model.save_pretrained(lora_dir)
    tokenizer.save_pretrained(lora_dir)

    print("PIPELINE_STAGE_COMPLETE=training")


if __name__ == "__main__":
    main()
