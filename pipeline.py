"""
Unified pipeline: Training → Export GGUF → MagicQuant → HF Upload.

Improvements over v1:
  1. Merged+Convert collapsed into single "Export" stage via save_pretrained_gguf
  2. Completion-only training (masks system prompt & user messages)
  3. Dataset validation pre-flight check before training
  4. Auto-install llama.cpp if not found (needed by MagicQuant for real PPL probing)
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ── Configuration ────────────────────────────────────────────────────────────

@dataclass
class TrainingConfig:
    model_name: str = "Tesslate/OmniCoder-9B"
    dataset_path: str = "zeroclaw_training_data.jsonl"
    max_seq_length: int = 8192
    load_in_4bit: bool = True
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    use_rslora: bool = True
    num_train_epochs: int = 3
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 4
    learning_rate: float = 2e-4
    lr_scheduler_type: str = "cosine"
    warmup_ratio: float = 0.05
    optim: str = "adamw_8bit"


@dataclass
class ExportConfig:
    """Single stage: merge LoRA + convert to GGUF."""
    gguf_type: str = "bf16"  # bf16, f16, q8_0, etc.
    also_save_merged: bool = False  # optionally also save HF safetensors


@dataclass
class MagicQuantConfig:
    target_base_quant: str = "MXFP4_MOE"
    generations: int = 50
    population_size: int = 100
    tiers: list[str] = field(default_factory=lambda: ["Q4", "Q5", "Q6"])
    verify: bool = False
    llamacpp_path: Optional[str] = None


@dataclass
class UploadConfig:
    repo_id: str = ""
    private: bool = True
    base_model: str = ""
    license: str = "apache-2.0"
    upload_lora: bool = False
    upload_merged: bool = False
    upload_gguf: bool = True


@dataclass
class PipelineConfig:
    output_dir: str = "./output"
    training: TrainingConfig = field(default_factory=TrainingConfig)
    export: Optional[ExportConfig] = field(default_factory=ExportConfig)
    magicquant: Optional[MagicQuantConfig] = field(default_factory=MagicQuantConfig)
    upload: Optional[UploadConfig] = None


# ── Artifact paths ───────────────────────────────────────────────────────────

class Artifacts:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def lora_dir(self) -> Path:
        return self.output_dir / "lora_adapters"

    @property
    def merged_dir(self) -> Path:
        return self.output_dir / "merged_model"

    @property
    def bf16_gguf(self) -> Path:
        return self.output_dir / "model-bf16.gguf"

    @property
    def magicquant_dir(self) -> Path:
        return self.output_dir / "magicquant"


# ── Helpers ──────────────────────────────────────────────────────────────────

LogFn = Callable[[str, str], None]


def _default_log(msg: str, level: str = "info"):
    prefix = {"error": "ERROR", "warn": "WARN", "success": "OK", "stage": ">>>"}.get(level, "   ")
    print(f"[{prefix}] {msg}")


def _run(cmd: list[str], log: LogFn, env_extra: dict = None, cwd: str = None) -> int:
    env = os.environ.copy()
    env.update({
        "HSA_ENABLE_SDMA": "0",
        "PYTORCH_HIP_ALLOC_CONF": "backend:native,expandable_segments:True",
        "UNSLOTH_SKIP_TORCHVISION_CHECK": "1",
        "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
        "PYTHONUNBUFFERED": "1",
    })
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=env, cwd=cwd, text=True, bufsize=1,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
    proc.wait()
    return proc.returncode


def _find_python() -> str:
    return sys.executable


def _find_llamacpp(hint: Optional[str] = None) -> Optional[Path]:
    candidates = [
        hint,
        os.environ.get("LLAMACPP_PATH"),
        str(Path.home() / "llama.cpp"),
        "./llama.cpp",
        "/usr/local",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        for sub in [p / "convert_hf_to_gguf.py", p / "bin" / "convert_hf_to_gguf.py",
                     p / "build" / "bin" / "llama-perplexity"]:
            if sub.exists():
                return p
    return None


# ── Improvement #3: Dataset validation ───────────────────────────────────────

def validate_dataset(dataset_path: str, log: LogFn) -> bool:
    """Pre-flight check on the training dataset before committing GPU time."""
    log("Validating dataset", "stage")
    path = Path(dataset_path)

    if not path.exists():
        log(f"Dataset not found: {path}", "error")
        return False

    if path.stat().st_size == 0:
        log("Dataset file is empty", "error")
        return False

    errors = []
    warnings = []
    tool_calls = 0
    roles_seen = set()
    n = 0

    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ex = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"Line {i}: invalid JSON — {e}")
                if len(errors) >= 5:
                    errors.append("(stopping after 5 errors)")
                    break
                continue

            n += 1

            if "messages" not in ex:
                errors.append(f"Line {i}: missing 'messages' field")
                continue

            msgs = ex["messages"]
            if not isinstance(msgs, list) or len(msgs) < 2:
                errors.append(f"Line {i}: 'messages' must be a list with >= 2 entries")
                continue

            for j, msg in enumerate(msgs):
                if "role" not in msg or "content" not in msg:
                    errors.append(f"Line {i}, message {j}: missing 'role' or 'content'")
                    break
                roles_seen.add(msg["role"])

            # Count tool calls for coverage stats
            for msg in msgs:
                if msg.get("role") == "assistant" and "<tool_call>" in msg.get("content", ""):
                    tool_calls += 1

    if errors:
        for e in errors:
            log(f"  {e}", "error")
        log(f"Dataset validation failed with {len(errors)} errors", "error")
        return False

    # Warnings
    if n < 10:
        warnings.append(f"Only {n} examples — consider adding more for better generalization")
    if "system" not in roles_seen:
        warnings.append("No 'system' role found — model may not learn system prompt behavior")
    if "assistant" not in roles_seen:
        warnings.append("No 'assistant' role found — nothing for the model to learn")

    for w in warnings:
        log(f"  Warning: {w}", "warn")

    # Stats
    log(f"  Examples: {n}")
    log(f"  Roles: {sorted(roles_seen)}")
    log(f"  Tool call turns: {tool_calls}")
    log(f"  File size: {path.stat().st_size / 1024:.1f} KB")
    log("Dataset validation passed", "success")
    return True


# ── Improvement #4: Auto-install llama.cpp ───────────────────────────────────

def ensure_llamacpp(hint: Optional[str], log: LogFn) -> Optional[Path]:
    """Find llama.cpp, or auto-install it if missing."""
    found = _find_llamacpp(hint)
    if found:
        log(f"llama.cpp found at: {found}")
        return found

    install_dir = Path.home() / "llama.cpp"
    log("llama.cpp not found — auto-installing", "stage")

    # Clone
    log("Cloning llama.cpp from GitHub...")
    rc = _run(["git", "clone", "--depth", "1", "https://github.com/ggml-org/llama.cpp.git", str(install_dir)], log)
    if rc != 0:
        log("Failed to clone llama.cpp", "error")
        return None

    # Build
    log("Building llama.cpp (cmake)...")
    build_dir = install_dir / "build"
    rc = _run(["cmake", "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release", str(install_dir)], log)
    if rc != 0:
        log("cmake configure failed", "error")
        return None

    import multiprocessing
    jobs = str(multiprocessing.cpu_count())
    rc = _run(["cmake", "--build", str(build_dir), "-j", jobs], log)
    if rc != 0:
        log("cmake build failed", "error")
        return None

    log(f"llama.cpp installed at: {install_dir}", "success")
    return install_dir


# ── Stage: Training (with completion-only loss) ─────────────────────────────

def stage_training(config: PipelineConfig, artifacts: Artifacts, log: LogFn) -> bool:
    """Run Unsloth QLoRA training with completion-only loss."""
    # Improvement #3: validate first
    if not validate_dataset(config.training.dataset_path, log):
        return False

    log("Starting Unsloth QLoRA training (completion-only)", "stage")
    tc = config.training

    # Improvement #2: completion_only_loss=True in SFTTrainer
    # We pass messages directly to SFTTrainer (not pre-formatted text)
    # so TRL can compute the completion_mask from the chat template.
    script = f'''
import os
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

import torch
from datasets import load_dataset
from unsloth import FastLanguageModel
from trl import SFTTrainer, SFTConfig

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{tc.model_name}",
    max_seq_length={tc.max_seq_length},
    load_in_4bit={tc.load_in_4bit},
)

model = FastLanguageModel.get_peft_model(
    model,
    r={tc.lora_r}, lora_alpha={tc.lora_alpha}, lora_dropout={tc.lora_dropout},
    target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    use_rslora={tc.use_rslora},
    use_gradient_checkpointing="unsloth",
)

trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
total = sum(p.numel() for p in model.parameters())
print(f"Trainable: {{trainable:,}} / {{total:,}} ({{100*trainable/total:.2f}}%)")

dataset = load_dataset("json", data_files="{tc.dataset_path}", split="train")
print(f"Dataset: {{len(dataset)}} examples")

# Completion-only loss: mask everything except assistant responses.
# Auto-detect the assistant header from the chat template by rendering a
# minimal conversation and extracting the text that precedes the assistant content.
from trl import DataCollatorForCompletionOnlyLM

_probe = tokenizer.apply_chat_template(
    [{{"role": "user", "content": "X"}}, {{"role": "assistant", "content": "Y"}}],
    tokenize=False, add_generation_prompt=False,
)
# Find the marker text between the user turn and the assistant content "Y"
_y_pos = _probe.rfind("Y")
# Walk backwards from "Y" to find the start of the assistant header
# Skip any <think>...</think> blocks that some models inject
_header_end = _y_pos
_before_y = _probe[:_y_pos]
# Strip think blocks if present (e.g. Qwen3.5 inserts <think>...</think> wrappers)
import re as _re
_before_y_clean = _re.sub(r"<think>.*?</think>\\s*", "", _before_y, flags=_re.DOTALL)
# Find where the previous turn ends (look for the last end-of-turn marker before assistant)
# Common end markers: <|im_end|>, <|eot_id|>, <end_of_turn>, </s>
_end_markers = ["<|im_end|>", "<|eot_id|>", "<end_of_turn>", "</s>"]
_last_end = -1
_marker_len = 0
for _em in _end_markers:
    _p = _before_y_clean.rfind(_em)
    if _p > _last_end:
        _last_end = _p
        _marker_len = len(_em)
if _last_end >= 0:
    response_template = _before_y_clean[_last_end + _marker_len:].lstrip("\\n")
else:
    response_template = _before_y_clean.split("\\n")[-1]

print(f"Auto-detected response template: {{repr(response_template)}}")
response_ids = tokenizer.encode(response_template, add_special_tokens=False)
collator = DataCollatorForCompletionOnlyLM(
    response_template=response_ids,
    tokenizer=tokenizer,
)

training_args = SFTConfig(
    output_dir="{config.output_dir}",
    num_train_epochs={tc.num_train_epochs},
    per_device_train_batch_size={tc.per_device_train_batch_size},
    gradient_accumulation_steps={tc.gradient_accumulation_steps},
    learning_rate={tc.learning_rate},
    lr_scheduler_type="{tc.lr_scheduler_type}",
    warmup_ratio={tc.warmup_ratio},
    optim="{tc.optim}",
    weight_decay=0.01, max_grad_norm=1.0,
    fp16=False, bf16=True,
    logging_steps=1, save_strategy="epoch", seed=42,
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={{"use_reentrant": False}},
    report_to="none",
    max_seq_length={tc.max_seq_length},
    dataset_text_field="text",
)

# Pre-format with chat template
def fmt(ex):
    ex["text"] = tokenizer.apply_chat_template(
        ex["messages"], tokenize=False, add_generation_prompt=False,
    )
    return ex

dataset = dataset.map(fmt)

trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, train_dataset=dataset,
    args=training_args,
    data_collator=collator,
)

stats = trainer.train()
print(f"PIPELINE_TRAINING_LOSS={{stats.training_loss:.4f}}")

lora_dir = "{artifacts.lora_dir}"
model.save_pretrained(lora_dir)
tokenizer.save_pretrained(lora_dir)
print("PIPELINE_STAGE_COMPLETE=training")
'''

    script_path = artifacts.output_dir / "_stage_train.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script)

    rc = _run([_find_python(), "-u", str(script_path)], log, cwd=str(Path.cwd()))
    if rc != 0:
        log(f"Training failed (exit code {rc})", "error")
        return False

    if not artifacts.lora_dir.exists():
        log("LoRA adapters directory not found after training", "error")
        return False

    log("Training complete", "success")
    return True


# ── Stage: Export (merge + GGUF in one shot) ─────────────────────────────────

def stage_export(config: PipelineConfig, artifacts: Artifacts, log: LogFn) -> bool:
    """Merge LoRA + export. Adapts output based on downstream stages.

    - MagicQuant enabled: merge to safetensors (MagicQuant reads them directly)
    - MagicQuant disabled: merge + convert to GGUF in one shot
    """
    ec = config.export
    mq_enabled = config.magicquant is not None

    if not artifacts.lora_dir.exists():
        log("No LoRA adapters found — run training first", "error")
        return False

    if mq_enabled:
        log("Merging LoRA to safetensors (MagicQuant will handle GGUF creation)", "stage")
        script = f'''
import os
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

from unsloth import FastLanguageModel
print("Loading model with LoRA adapters...")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{artifacts.lora_dir}", max_seq_length=4096, load_in_4bit=False,
)
print("Merging to safetensors...")
model.save_pretrained_merged("{artifacts.merged_dir}", tokenizer, save_method="merged_16bit")
print("PIPELINE_STAGE_COMPLETE=export")
'''
    else:
        log(f"Exporting to {ec.gguf_type.upper()} GGUF (merge + convert)", "stage")
        gguf_out = str(artifacts.output_dir)
        save_merged = "True" if ec.also_save_merged else "False"
        script = f'''
import os, shutil
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

from pathlib import Path
from unsloth import FastLanguageModel
print("Loading model with LoRA adapters...")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="{artifacts.lora_dir}", max_seq_length=4096, load_in_4bit=False,
)

if {save_merged}:
    print("Also saving merged HF safetensors...")
    model.save_pretrained_merged("{artifacts.merged_dir}", tokenizer, save_method="merged_16bit")

print("Merging + converting to {ec.gguf_type.upper()} GGUF...")
model.save_pretrained_gguf("{gguf_out}", tokenizer, quantization_method="{ec.gguf_type}")

# Rename to predictable path
dst = Path("{artifacts.bf16_gguf}")
for d in [Path("{gguf_out}"), Path("{gguf_out}_gguf")]:
    if d.exists():
        for f in d.glob("*.gguf"):
            if f != dst:
                shutil.move(str(f), str(dst))
                break
if dst.exists():
    size = dst.stat().st_size / 1e9
    print(f"GGUF: {{dst}} ({{size:.1f}} GB)")
print("PIPELINE_STAGE_COMPLETE=export")
'''

    script_path = artifacts.output_dir / "_stage_export.py"
    script_path.write_text(script)

    rc = _run([_find_python(), "-u", str(script_path)], log)
    if rc != 0:
        log(f"Export failed (exit code {rc})", "error")
        return False

    if mq_enabled and artifacts.merged_dir.exists():
        log(f"Merged safetensors ready at {artifacts.merged_dir}", "success")
    elif not mq_enabled and artifacts.bf16_gguf.exists():
        size_gb = artifacts.bf16_gguf.stat().st_size / 1e9
        log(f"BF16 GGUF: {artifacts.bf16_gguf} ({size_gb:.1f} GB)", "success")

    log("Export complete", "success")
    return True


# ── Stage: MagicQuant ────────────────────────────────────────────────────────

def stage_magicquant(config: PipelineConfig, artifacts: Artifacts, log: LogFn) -> bool:
    """Run MagicQuant evolutionary search and generate hybrid GGUFs.

    Reads from merged safetensors (preferred) or BF16 GGUF (fallback).
    """
    log("Starting MagicQuant evolutionary quantization", "stage")

    # Determine source: prefer merged safetensors, fall back to GGUF
    if artifacts.merged_dir.exists():
        source_path = str(artifacts.merged_dir)
        log(f"Source: merged safetensors at {source_path}")
    elif artifacts.bf16_gguf.exists():
        source_path = str(artifacts.bf16_gguf)
        log(f"Source: BF16 GGUF at {source_path}")
    else:
        log("No merged model or BF16 GGUF found — run export first", "error")
        return False

    mc = config.magicquant

    # Improvement #4: auto-install llama.cpp if needed
    llamacpp = ensure_llamacpp(mc.llamacpp_path, log)

    try:
        from magicquant.orchestrator import MagicQuantOrchestrator

        orch = MagicQuantOrchestrator(
            source_model_path=source_path,
            output_dir=str(artifacts.magicquant_dir),
            llamacpp_path=str(llamacpp) if llamacpp else None,
        )

        log(f"Search: generations={mc.generations}, population={mc.population_size}, base={mc.target_base_quant}")

        best_configs, tiered = orch.run_full_search(
            target_base_quant=mc.target_base_quant,
            max_generations=mc.generations,
            population_size=mc.population_size,
            verbose=True,
        )

        if not tiered:
            log("Evolutionary search produced no viable configurations", "error")
            return False

        log(f"Search complete — tiers: {list(tiered.keys())}")

        model_name = config.training.model_name.split("/")[-1]
        paths = orch.generate_tiered_models(
            tiered=tiered,
            model_name_prefix=model_name,
            tiers=mc.tiers,
            verify=mc.verify,
        )

        valid_paths = [p for p in paths if p]
        for p in valid_paths:
            size_gb = Path(p).stat().st_size / 1e9
            log(f"  {Path(p).name} ({size_gb:.1f} GB)")
        log(f"Generated {len(valid_paths)} hybrid GGUF files", "success")
        return True

    except Exception as e:
        log(f"MagicQuant error: {e}", "error")
        import traceback
        log(traceback.format_exc(), "error")
        return False


# ── Stage: Upload ────────────────────────────────────────────────────────────

def stage_upload(config: PipelineConfig, artifacts: Artifacts, log: LogFn) -> bool:
    """Upload artifacts to HuggingFace Hub."""
    log("Uploading to HuggingFace Hub", "stage")

    uc = config.upload
    if not uc or not uc.repo_id:
        log("No repo_id configured for upload", "error")
        return False

    try:
        from huggingface_hub import HfApi, ModelCard, ModelCardData
    except ImportError:
        log("huggingface_hub not installed", "error")
        return False

    api = HfApi()

    log(f"Creating/verifying repo: {uc.repo_id}")
    try:
        api.create_repo(repo_id=uc.repo_id, repo_type="model", private=uc.private, exist_ok=True)
    except Exception as e:
        log(f"Failed to create repo: {e}", "error")
        return False

    files_to_upload = []

    if uc.upload_lora and artifacts.lora_dir.exists():
        for f in artifacts.lora_dir.iterdir():
            if f.is_file():
                files_to_upload.append((f, f"lora/{f.name}"))

    if uc.upload_merged and artifacts.merged_dir.exists():
        for f in artifacts.merged_dir.iterdir():
            if f.is_file():
                files_to_upload.append((f, f"merged/{f.name}"))

    if uc.upload_gguf:
        gguf_files = []
        if artifacts.magicquant_dir.exists():
            gguf_files = list(artifacts.magicquant_dir.glob("*.gguf"))
        if not gguf_files and artifacts.bf16_gguf.exists():
            gguf_files = [artifacts.bf16_gguf]
        for f in gguf_files:
            files_to_upload.append((f, f.name))

    if not files_to_upload:
        log("No files to upload", "warn")
        return False

    log(f"Uploading {len(files_to_upload)} files")

    # Model card
    base_model = uc.base_model or config.training.model_name
    tc = config.training

    gguf_rows = ""
    for local, name in files_to_upload:
        if name.endswith(".gguf"):
            size = local.stat().st_size / 1e9
            gguf_rows += f"| {name} | {size:.1f} GB |\n"

    card_data = ModelCardData(
        license=uc.license,
        library_name="llama.cpp",
        base_model=base_model,
        pipeline_tag="text-generation",
        tags=["gguf", "quantized", "unsloth", "magicquant"],
    )

    card_content = f"""---
{card_data.to_yaml()}
---

# {uc.repo_id.split('/')[-1]}

Fine-tuned and quantized from [{base_model}](https://huggingface.co/{base_model}).

## Pipeline

- **Training**: Unsloth QLoRA (r={tc.lora_r}, alpha={tc.lora_alpha}, rsLoRA, completion-only loss)
- **Quantization**: MagicQuant hybrid evolutionary search

## Files

| File | Size |
|------|------|
{gguf_rows}

## Usage

```bash
llama-cli -m <filename>.gguf -p "Your prompt here"
```

---
*Generated with the Unsloth Pipeline*
"""

    try:
        card = ModelCard(card_content)
        card.push_to_hub(uc.repo_id)
        log("Model card uploaded")
    except Exception as e:
        log(f"Model card upload failed: {e}", "warn")

    for i, (local_path, repo_path) in enumerate(files_to_upload, 1):
        size_gb = local_path.stat().st_size / 1e9
        log(f"[{i}/{len(files_to_upload)}] {repo_path} ({size_gb:.1f} GB)...")
        try:
            api.upload_file(
                path_or_fileobj=str(local_path),
                path_in_repo=repo_path,
                repo_id=uc.repo_id,
                repo_type="model",
                commit_message=f"Add {repo_path}",
            )
            log(f"  Uploaded {repo_path}", "success")
        except Exception as e:
            log(f"Failed to upload {repo_path}: {e}", "error")
            return False

    log(f"All files uploaded to https://huggingface.co/{uc.repo_id}", "success")
    return True


# ── Pipeline runner ──────────────────────────────────────────────────────────

STAGES = [
    ("training",   stage_training),
    ("export",     stage_export),
    ("magicquant", stage_magicquant),
    ("upload",     stage_upload),
]


def run_pipeline(config: PipelineConfig, log: LogFn = _default_log) -> dict[str, bool]:
    """Run the full pipeline. Returns {stage_name: success/None(skipped)}."""
    artifacts = Artifacts(config.output_dir)
    results = {}

    enabled = set()
    if config.training is not None:
        enabled.add("training")
    if config.export is not None:
        enabled.add("export")
    if config.magicquant is not None:
        enabled.add("magicquant")
    if config.upload is not None:
        enabled.add("upload")

    log(f"Pipeline: {' → '.join(s for s, _ in STAGES if s in enabled)}", "stage")

    for stage_name, stage_fn in STAGES:
        if stage_name not in enabled:
            log(f"Skipping {stage_name}")
            results[stage_name] = None
            continue

        ok = stage_fn(config, artifacts, log)
        results[stage_name] = ok
        if not ok:
            log(f"Pipeline stopped at {stage_name}", "error")
            break

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Unsloth Pipeline")
    parser.add_argument("--config", type=str, help="YAML config file")
    parser.add_argument("--output-dir", type=str, default="./output")
    parser.add_argument("--model", type=str)
    parser.add_argument("--dataset", type=str)
    parser.add_argument("--no-export", action="store_true")
    parser.add_argument("--no-magicquant", action="store_true")
    parser.add_argument("--upload-to", type=str, help="HF repo ID")
    parser.add_argument("--llamacpp-path", type=str)
    args = parser.parse_args()

    cfg = PipelineConfig(output_dir=args.output_dir)

    if args.config:
        import yaml
        with open(args.config) as f:
            data = yaml.safe_load(f)
        if "training" in data:
            for k, v in data["training"].items():
                setattr(cfg.training, k, v)

    if args.model:
        cfg.training.model_name = args.model
    if args.dataset:
        cfg.training.dataset_path = args.dataset
    if args.no_export:
        cfg.export = None
    if args.no_magicquant:
        cfg.magicquant = None
    if args.upload_to:
        cfg.upload = UploadConfig(repo_id=args.upload_to)
    if args.llamacpp_path and cfg.magicquant:
        cfg.magicquant.llamacpp_path = args.llamacpp_path

    results = run_pipeline(cfg)

    print("\n" + "=" * 50)
    print("Pipeline Results:")
    for stage, ok in results.items():
        sym = "✓" if ok else ("—" if ok is None else "✗")
        print(f"  {sym} {stage}")
