"""Importable entry module for the QLoRA training stage (audit H2).

Before this existed, the entire training body lived as an escaped f-string inside
``core/services.py:TrainingService.build_script`` — no IDE/lint/type support and
tracebacks pointed at an anonymous ``_stage_train.py``. Now ``build_script``
emits a ~10-line shim that writes a JSON config and runs ``python -m
core._train_entry cfg.json``; the real logic lives here as ordinary Python.

Design constraints (mirrors core/onnx_quark.py):
  * Module import is dependency-free (stdlib only). torch / transformers / trl /
    peft are imported lazily inside ``run()`` so this module — and its config
    parsing — is unit-testable without a GPU (tests/test_stage_entries.py).
  * The ROCm env vars are set at the top of ``run()`` *before* torch is
    imported, preserving the original ordering requirement.
  * Dataset format normalization is delegated to core/dataset_format.py so every
    input shape (messages / text / prompt-completion / alpaca) yields the same
    chat structure (audit L-dataset-format).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ── ROCm env preamble (must run before torch import in run()) ─────────────────
_ROCM_ENV = {
    "HSA_ENABLE_SDMA": "0",
    "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
    "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
}

# ChatML fallback for models that ship no chat template (e.g. base Gemma).
_CHATML_TEMPLATE = (
    "{% for message in messages %}"
    "{{ '<|im_start|>' + message['role'] + '\n' + message['content'] + '<|im_end|>' + '\n' }}"
    "{% endfor %}"
    "{% if add_generation_prompt %}{{ '<|im_start|>assistant\n' }}{% endif %}"
)

# Map a file suffix to a HF ``load_dataset`` builder name.
_EXT_TO_BUILDER = {"jsonl": "json", "json": "json", "csv": "csv", "parquet": "parquet"}


def hf_cache_probe(model_id: str) -> None:
    """Log whether the model is local / cached / will be downloaded (info only)."""
    if Path(model_id).exists():
        print(f"Loading from local path: {model_id}", flush=True)
        return
    try:
        from huggingface_hub import scan_cache_dir

        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                print(
                    f"Model found in HF cache ({repo.size_on_disk / 1e9:.1f} GB) — "
                    "no download needed",
                    flush=True,
                )
                return
        print(f"Model not in cache — will download from HuggingFace: {model_id}", flush=True)
    except Exception:
        print(f"Loading model: {model_id}", flush=True)


def parse_config(cfg_path: str) -> dict:
    """Read the JSON config the shim wrote. Pure (no torch); unit-testable."""
    return json.loads(Path(cfg_path).read_text())


def resolve_sources(cfg: dict) -> list[str]:
    """Resolve the dataset source list from a config dict.

    Prefers ``datasets``; falls back to a single ``dataset_path``; finally the
    historical default file. Mirrors TrainingService's resolution so the entry
    module and the old inline behavior agree.
    """
    datasets = cfg.get("datasets") or []
    if datasets:
        return list(datasets)
    if cfg.get("dataset_path"):
        return [cfg["dataset_path"]]
    return ["data/zeroclaw_training_data.jsonl"]


def _load_one_source(src: str, pipeline_root: str, load_dataset):
    """Load a single dataset source (local file or HF id with optional split/config)."""
    src = src.strip()
    if not src:
        return None
    local = Path(src)
    if not local.is_absolute():
        local = Path(pipeline_root) / local
    if local.exists():
        ext = local.suffix.lstrip(".")
        builder = _EXT_TO_BUILDER.get(ext, "json")
        ds = load_dataset(builder, data_files=str(local), split="train")
        print(f"Loaded local: {src} ({len(ds)} examples)", flush=True)
        return ds
    # HF dataset id; support optional [split] suffix and :config name.
    split = "train"
    cfg_name = None
    clean = src
    if "[" in clean and clean.endswith("]"):
        clean, split = clean[:-1].split("[", 1)
    if ":" in clean and not clean.startswith("/") and "." not in clean.split("/")[-1]:
        clean, cfg_name = clean.rsplit(":", 1)
    kwargs = {"split": split}
    if cfg_name:
        kwargs["name"] = cfg_name
    ds = load_dataset(clean, **kwargs)
    print(f"Loaded HF: {src} ({len(ds)} examples)", flush=True)
    return ds


def load_and_normalize_dataset(cfg: dict, pipeline_root: str):
    """Load every source, normalize each to the chat ``messages`` schema, combine.

    Returns a ``datasets.Dataset`` with a single ``messages`` column (uniform
    ``List(struct{role, content})`` Arrow schema), regardless of input shape.
    """
    from datasets import Dataset, concatenate_datasets, load_dataset

    try:
        import dataset_format as df
    except ImportError:  # pragma: no cover - package-import fallback
        from core import dataset_format as df

    loaded = []
    for src in resolve_sources(cfg):
        ds = _load_one_source(src, pipeline_root, load_dataset)
        if ds is None:
            continue
        rows = df.normalize_dataset(ds)
        loaded.append(Dataset.from_list(rows))

    if len(loaded) == 1:
        dataset = loaded[0]
    elif len(loaded) > 1:
        dataset = concatenate_datasets(loaded).shuffle(seed=42)
        print(f"Combined: {len(dataset)} examples from {len(loaded)} sources", flush=True)
    else:
        raise ValueError("No datasets loaded")
    print(f"Dataset: {len(dataset)} examples", flush=True)
    return dataset


def _report_token_lengths(dataset, tokenizer, max_seq_length: int) -> None:
    lengths = sorted(len(tokenizer.encode(ex["text"])) for ex in dataset)
    p50 = lengths[len(lengths) // 2]
    p90 = lengths[int(len(lengths) * 0.9)]
    p99 = lengths[int(len(lengths) * 0.99)]
    longest = lengths[-1]
    print(f"Token lengths ({len(lengths)} examples):", flush=True)
    print(f"  Median: {p50} tokens", flush=True)
    print(f"  90% of examples are under: {p90} tokens", flush=True)
    print(f"  99% of examples are under: {p99} tokens", flush=True)
    print(f"  Longest example: {longest} tokens", flush=True)
    print(f"  Current max_length: {max_seq_length} tokens", flush=True)
    if p90 < max_seq_length // 2:
        print(
            f"  Tip: 90% of examples fit in {p90} tokens — lowering max_length to "
            f"~{int(p90 * 1.1)} would save memory and speed up training",
            flush=True,
        )
    over = sum(1 for l in lengths if l > max_seq_length)
    if over:
        print(
            f"  Warning: {over} examples ({100 * over / len(lengths):.1f}%) are longer "
            "than max_length and will be truncated",
            flush=True,
        )


def run(cfg_path: str | None = None) -> None:
    """Execute the training stage from a JSON config file."""
    import os

    for k, v in _ROCM_ENV.items():
        os.environ.setdefault(k, v)

    if cfg_path is None:
        cfg_path = sys.argv[1]
    cfg = parse_config(cfg_path)

    pipeline_root = cfg["pipeline_root"]
    core_path = str(Path(pipeline_root) / "core")
    if core_path not in sys.path:
        sys.path.insert(0, core_path)

    import torch  # noqa: F401  (kept for parity / fail-fast on missing ROCm torch)

    from fast_train_zeroclaw import fast_load_quantized_model, find_latest_checkpoint
    from trl import SFTTrainer, SFTConfig
    from peft import LoraConfig, get_peft_model

    try:
        import dataset_format as df
    except ImportError:  # pragma: no cover
        from core import dataset_format as df

    model_name = cfg["model_name"]
    output_dir = cfg["output_dir"]
    max_seq_length = cfg["max_seq_length"]

    from fast_export import detect_gguf_source

    if detect_gguf_source(model_name) is not None:
        raise RuntimeError(
            f"Training requires safetensors weights, but {model_name!r} is a "
            "GGUF source. Disable the training and export stages — GGUF "
            "sources pass straight through to MagicQuant/ROCmFPX — or point "
            "--model at the original safetensors repo."
        )

    hf_cache_probe(model_name)
    model, tokenizer = fast_load_quantized_model(model_name)

    # Ensure the tokenizer has a chat template (some models ship none).
    if not getattr(tokenizer, "chat_template", None):
        print("WARNING: tokenizer has no chat_template — applying ChatML default", flush=True)
        tokenizer.chat_template = _CHATML_TEMPLATE

    # Manual kbit prep — avoids fp32 upcast of MoE experts.
    for param in model.parameters():
        param.requires_grad = False
    for name, module in model.named_modules():
        if "norm" in name.lower() or "layernorm" in name.lower():
            module.to(torch.float32)
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    print(f"After manual kbit prep: {torch.cuda.memory_allocated() / 1e9:.1f} GB", flush=True)

    lora_config = LoraConfig(
        r=cfg["lora_r"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_rslora=cfg["use_rslora"],
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Trainable: {trainable:,} / {total:,} ({100 * trainable / total:.2f}%)", flush=True)

    dataset = load_and_normalize_dataset(cfg, pipeline_root)

    def fmt(ex):
        ex["text"] = df.messages_to_text(ex["messages"], tokenizer)
        return ex

    dataset = dataset.map(fmt)

    _report_token_lengths(dataset, tokenizer, max_seq_length)

    resume_ckpt = find_latest_checkpoint(output_dir)
    if resume_ckpt:
        print(f"Resuming from checkpoint: {resume_ckpt}", flush=True)

    packing = cfg["packing"]
    sft_kwargs = dict(
        output_dir=output_dir,
        num_train_epochs=cfg["num_train_epochs"],
        per_device_train_batch_size=cfg["per_device_train_batch_size"],
        gradient_accumulation_steps=cfg["gradient_accumulation_steps"],
        learning_rate=cfg["learning_rate"],
        lr_scheduler_type=cfg["lr_scheduler_type"],
        optim=cfg["optim"],
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
        max_length=max_seq_length,
        dataset_text_field="text",
        packing=packing,
        completion_only_loss=not packing,
    )
    # Warmup: ratio preferred; steps only when ratio is absent (audit M-warmup).
    if cfg.get("warmup_ratio") is not None:
        sft_kwargs["warmup_ratio"] = cfg["warmup_ratio"]
    else:
        sft_kwargs["warmup_steps"] = cfg["warmup_steps"]

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(**sft_kwargs),
    )
    stats = trainer.train(resume_from_checkpoint=resume_ckpt)
    print(f"Final loss: {stats.training_loss:.4f}", flush=True)

    lora_dir = output_dir + "/lora_adapters"
    model.save_pretrained(lora_dir)
    tokenizer.save_pretrained(lora_dir)
    print("PIPELINE_STAGE_COMPLETE=training", flush=True)


if __name__ == "__main__":
    run()
