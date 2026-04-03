"""
Service layer for pipeline stages.

Extracts business logic from ui/app.py route handlers into testable,
reusable service classes. Each service accepts a config and a progress
callback, handles subprocess execution, and returns success/failure.

Usage from the UI:
    svc = TrainingService(pipeline_root, venv_python)
    ok = await svc.run(cfg, run_script_fn)
"""

import json
import os
from pathlib import Path
from typing import Callable, Awaitable, Optional

# Type alias for the async subprocess runner used by the UI.
# Signature: (script_text, output_dir) -> exit_code
RunScriptFn = Callable[[str, str], Awaitable[int]]

# Type alias for the async log callback used by the UI.
AsyncLogFn = Callable[[str, str], Awaitable[None]]


def _env_preamble() -> str:
    """Return the standard ROCm environment setup block for subprocess scripts."""
    return (
        'import os\n'
        'os.environ["HSA_ENABLE_SDMA"] = "0"\n'
        'os.environ["PYTORCH_HIP_ALLOC_CONF"] = '
        '"backend:native,expandable_segments:True"\n'
        'os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"\n'
        'os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"\n'
    )


def _hf_cache_check(model_id_repr: str) -> str:
    """Return an HF cache probe snippet for subprocess scripts."""
    return (
        "from pathlib import Path as _P\n"
        f"_model_id = {model_id_repr}\n"
        "_is_local = _P(_model_id).exists()\n"
        "if _is_local:\n"
        '    print(f"Loading from local path: {_model_id}")\n'
        "else:\n"
        "    try:\n"
        "        from huggingface_hub import scan_cache_dir\n"
        "        _cached = False\n"
        "        for _repo in scan_cache_dir().repos:\n"
        "            if _repo.repo_id == _model_id:\n"
        "                _size_gb = _repo.size_on_disk / 1e9\n"
        '                print(f"Model found in HF cache '
        '({_size_gb:.1f} GB) — no download needed")\n'
        "                _cached = True\n"
        "                break\n"
        "        if not _cached:\n"
        '            print(f"Model not in cache — will download '
        'from HuggingFace: {_model_id}")\n'
        "    except Exception:\n"
        '        print(f"Loading model: {_model_id}")\n'
    )


class TrainingService:
    """Orchestrates the QLoRA training subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_script(
        self,
        *,
        model_name: str,
        dataset_path: str,
        output_dir: str,
        max_seq_length: int,
        lora_r: int,
        lora_alpha: int,
        lora_dropout: float,
        use_rslora: bool,
        num_train_epochs: int,
        per_device_train_batch_size: int,
        gradient_accumulation_steps: int,
        learning_rate: float,
        lr_scheduler_type: str,
        warmup_steps: int,
        optim: str,
    ) -> str:
        """Generate the training subprocess script text."""
        core_path = repr(str(self.pipeline_root / "core"))
        pipeline_root_str = repr(str(self.pipeline_root))

        script = _env_preamble()
        script += _hf_cache_check(repr(model_name))
        script += (
            f"\nimport sys, torch\n"
            f"sys.path.insert(0, str(_P({core_path})))\n"
            f"\n"
            f"from fast_train_zeroclaw import "
            f"fast_load_quantized_model, find_latest_checkpoint\n"
            f"from datasets import load_dataset\n"
            f"from trl import SFTTrainer, SFTConfig\n"
            f"from peft import LoraConfig, get_peft_model\n"
            f"\n"
            f"DEVICE = torch.device('cuda:0')\n"
            f"model, tokenizer = fast_load_quantized_model({repr(model_name)})\n"
            f"\n"
            f"# Manual kbit prep — avoids fp32 upcast of MoE experts\n"
            f"for param in model.parameters():\n"
            f"    param.requires_grad = False\n"
            f"for name, module in model.named_modules():\n"
            f'    if "norm" in name.lower() or "layernorm" in name.lower():\n'
            f"        module.to(torch.float32)\n"
            f"model.gradient_checkpointing_enable("
            f'gradient_checkpointing_kwargs={{"use_reentrant": False}})\n'
            f"model.enable_input_require_grads()\n"
            f'print(f"After manual kbit prep: '
            f'{{torch.cuda.memory_allocated()/1e9:.1f}} GB")\n'
            f"\n"
            f"lora_config = LoraConfig(\n"
            f"    r={lora_r}, lora_alpha={lora_alpha}, "
            f"lora_dropout={lora_dropout},\n"
            f'    target_modules=["q_proj","k_proj","v_proj","o_proj",'
            f'"gate_proj","up_proj","down_proj"],\n'
            f"    use_rslora={use_rslora}, task_type=\"CAUSAL_LM\",\n"
            f")\n"
            f"model = get_peft_model(model, lora_config)\n"
            f"\n"
            f"trainable = sum(p.numel() for p in model.parameters() "
            f"if p.requires_grad)\n"
            f"total = sum(p.numel() for p in model.parameters())\n"
            f'print(f"Trainable: {{trainable:,}} / {{total:,}} '
            f'({{100*trainable/total:.2f}}%)")\n'
            f"\n"
            f"_dataset_path = _P({repr(dataset_path)})\n"
            f"if not _dataset_path.is_absolute():\n"
            f"    _dataset_path = _P({pipeline_root_str}) / _dataset_path\n"
            f'dataset = load_dataset("json", '
            f'data_files=str(_dataset_path), split="train")\n'
            f'print(f"Dataset: {{len(dataset)}} examples")\n'
            f"\n"
            f"def fmt(ex):\n"
            f'    ex["text"] = tokenizer.apply_chat_template('
            f'ex["messages"], tokenize=False, '
            f"add_generation_prompt=False)\n"
            f"    return ex\n"
            f"dataset = dataset.map(fmt)\n"
            f"\n"
            f"resume_ckpt = find_latest_checkpoint({repr(output_dir)})\n"
            f"if resume_ckpt:\n"
            f'    print(f"Resuming from checkpoint: {{resume_ckpt}}")\n'
            f"\n"
            f"trainer = SFTTrainer(\n"
            f"    model=model, processing_class=tokenizer, "
            f"train_dataset=dataset,\n"
            f"    args=SFTConfig(\n"
            f"        output_dir={repr(output_dir)},\n"
            f"        num_train_epochs={num_train_epochs},\n"
            f"        per_device_train_batch_size="
            f"{per_device_train_batch_size},\n"
            f"        gradient_accumulation_steps="
            f"{gradient_accumulation_steps},\n"
            f"        learning_rate={learning_rate},\n"
            f"        lr_scheduler_type={repr(lr_scheduler_type)},\n"
            f"        warmup_steps={warmup_steps}, "
            f"optim={repr(optim)},\n"
            f"        weight_decay=0.01, max_grad_norm=1.0, "
            f"fp16=False, bf16=True,\n"
            f"        logging_steps=1, save_strategy=\"epoch\", "
            f"seed=42,\n"
            f"        gradient_checkpointing=True,\n"
            f"        gradient_checkpointing_kwargs="
            f'{{\"use_reentrant\": False}},\n'
            f'        report_to="none",\n'
            f"        max_length={max_seq_length},\n"
            f'        dataset_text_field="text",\n'
            f"        completion_only_loss=True,\n"
            f"    ),\n"
            f")\n"
            f"stats = trainer.train(resume_from_checkpoint=resume_ckpt)\n"
            f'print(f"Final loss: {{stats.training_loss:.4f}}")\n'
            f"\n"
            f"lora_dir = {repr(output_dir + '/lora_adapters')}\n"
            f"model.save_pretrained(lora_dir)\n"
            f"tokenizer.save_pretrained(lora_dir)\n"
            f'print("PIPELINE_STAGE_COMPLETE=training")\n'
        )
        return script


class ExportService:
    """Orchestrates the streaming LoRA merge / export subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_script(
        self,
        *,
        base_model_id: str,
        lora_source: Optional[str],
        has_lora: bool,
        merged_dir: str,
    ) -> str:
        """Generate the export subprocess script text."""
        core_path = repr(str(self.pipeline_root / "core"))

        script = _env_preamble()
        script += _hf_cache_check(repr(base_model_id))
        script += (
            f"\nimport sys\n"
            f"sys.path.insert(0, str(_P({core_path})))\n"
            f"from fast_export import streaming_merge\n"
            f"streaming_merge(\n"
            f"    model_id={repr(base_model_id)},\n"
            f"    lora_dir={repr(lora_source)} if {has_lora} else None,\n"
            f"    merged_dir={repr(merged_dir)},\n"
            f")\n"
            f'print("PIPELINE_STAGE_COMPLETE=export")\n'
        )
        return script


class MagicQuantService:
    """Orchestrates the MagicQuant evolutionary search subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_script(
        self,
        *,
        llamacpp_hint: str,
        pipeline_root_str: str,
        mq_source_override: str,
        out_abs_str: str,
        generations: int,
        population_size: int,
        target_base_quant: str,
        tiers_json: str,
        model_name: str,
    ) -> str:
        """Generate the MagicQuant subprocess script text."""
        script = (
            "import sys, os, subprocess, multiprocessing\n"
            "from pathlib import Path\n"
            "\n"
            "def find_llamacpp():\n"
            f"    for p in [{repr(llamacpp_hint)}, "
            f'os.environ.get("LLAMACPP_PATH",""),\n'
            f'              str(Path.home()/"llama.cpp"), '
            f'"./llama.cpp", "/usr/local"]:\n'
            f"        if not p: continue\n"
            f"        pp = Path(p)\n"
            f'        for sub in [pp/"convert_hf_to_gguf.py", '
            f'pp/"build"/"bin"/"llama-quantize"]:\n'
            f"            if sub.exists(): return str(pp)\n"
            f"    return None\n"
            f"\n"
            f"llamacpp = find_llamacpp()\n"
            f"if not llamacpp:\n"
            f'    install_dir = Path.home() / "llama.cpp"\n'
            f'    print("llama.cpp not found — auto-installing...")\n'
            f'    rc = subprocess.run(["git", "clone", "--depth", "1",\n'
            f'                         '
            f'"https://github.com/ggml-org/llama.cpp.git", '
            f"str(install_dir)]).returncode\n"
            f"    if rc == 0:\n"
            f'        build_dir = install_dir / "build"\n'
            f'        rc = subprocess.run(["cmake", "-B", '
            f'str(build_dir), "-DCMAKE_BUILD_TYPE=Release",\n'
            f"                             "
            f"str(install_dir)]).returncode\n"
            f"        if rc == 0:\n"
            f"            jobs = str(multiprocessing.cpu_count())\n"
            f'            rc = subprocess.run(["cmake", "--build", '
            f'str(build_dir), "-j", jobs]).returncode\n'
            f"    if rc == 0:\n"
            f"        llamacpp = str(install_dir)\n"
            f'        print(f"llama.cpp installed: {{llamacpp}}")\n'
            f"    else:\n"
            f'        print("Warning: llama.cpp install failed, '
            f'using heuristic probing")\n'
            f"        llamacpp = None\n"
            f"\n"
            f"print(f\"llama.cpp: "
            f"{{llamacpp or 'not found (heuristic mode)'}}\")\n"
            f"\n"
            f"sys.path.insert(0, str(Path({repr(pipeline_root_str)}) "
            f'/ "MagicQuant"))\n'
            f"from magicquant.orchestrator import "
            f"MagicQuantOrchestrator\n"
            f"import json\n"
            f"\n"
            f"override = {repr(mq_source_override)}\n"
            f"out_dir = Path({repr(out_abs_str)})\n"
            f"\n"
            f"def _resolve_source(override, out_dir):\n"
            f"    candidates = []\n"
            f"    if override:\n"
            f"        p = Path(override)\n"
            f"        if not p.is_absolute():\n"
            f"            candidates = [out_dir / override, "
            f"Path({repr(pipeline_root_str)}) / override]\n"
            f"        else:\n"
            f"            candidates = [p]\n"
            f"    if not candidates:\n"
            f"        candidates = [out_dir]\n"
            f"    for c in candidates:\n"
            f"        if c.is_dir():\n"
            f'            merged = c / "merged_model"\n'
            f"            if merged.exists() and "
            f'any(merged.glob("*.safetensors")):\n'
            f"                return str(merged)\n"
            f'            if any(c.glob("*.safetensors")):\n'
            f"                return str(c)\n"
            f'            gguf = c / "model-bf16.gguf"\n'
            f"            if gguf.exists():\n"
            f"                return str(gguf)\n"
            f"        elif c.is_file():\n"
            f"            return str(c)\n"
            f"    return None\n"
            f"\n"
            f"source = _resolve_source(override, out_dir)\n"
            f"if not source:\n"
            f'    print("Error: no source model found. Enable Export '
            f'or set a Source Model path in MagicQuant config.")\n'
            f"    sys.exit(1)\n"
            f'print(f"MagicQuant source: {{source}}")\n'
            f"\n"
            f"orch = MagicQuantOrchestrator(\n"
            f"    source_model_path=source,\n"
            f'    output_dir=str(out_dir / "magicquant"),\n'
            f"    llamacpp_path=llamacpp,\n"
            f")\n"
            f"\n"
            f'print(f"Search: generations={generations}, '
            f"population={population_size}, "
            f'base={target_base_quant}")\n'
            f"\n"
            f"best_configs, tiered = orch.run_full_search(\n"
            f"    target_base_quant={repr(target_base_quant)},\n"
            f"    max_generations={generations},\n"
            f"    population_size={population_size},\n"
            f"    verbose=True,\n"
            f")\n"
            f"\n"
            f"if not tiered:\n"
            f'    print("Error: no viable configurations found")\n'
            f"    sys.exit(1)\n"
            f"\n"
            f'print(f"Tiers found: {{list(tiered.keys())}}")\n'
            f"\n"
            f"tiers = json.loads({repr(tiers_json)})\n"
            f"paths = orch.generate_tiered_models(\n"
            f"    tiered=tiered, model_name_prefix={repr(model_name)}, "
            f"tiers=tiers, verify=False,\n"
            f")\n"
            f"valid = [p for p in paths if p]\n"
            f"for p in valid:\n"
            f"    size = os.path.getsize(p) / 1e9\n"
            f'    print(f"  {{Path(p).name}} ({{size:.1f}} GB)")\n'
            f'print(f"Generated {{len(valid)}} hybrid GGUF files")\n'
            f'print("PIPELINE_STAGE_COMPLETE=magicquant")\n'
        )
        return script


class UploadService:
    """Orchestrates the HuggingFace upload subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_script(
        self,
        *,
        repo_id: str,
        private: bool,
        license_id: str,
        upload_gguf: bool,
        upload_lora: bool,
        upload_merged: bool,
        upload_dataset: bool,
        base_model: str,
        dataset_name: str,
        lora_r: int,
        lora_alpha: int,
        lora_dropout: float,
        num_epochs: int,
        learning_rate: float,
        max_seq_length: int,
        batch_size: int,
        gradient_accumulation: int,
        optimizer: str,
        lr_scheduler: str,
        out_abs: str,
    ) -> str:
        """Generate the upload subprocess script text."""
        script = (
            "import sys\n"
            'sys.path.insert(0, "core")\n'
            "from hf_upload import HFUploadConfig, upload\n"
            "\n"
            "cfg = HFUploadConfig(\n"
            f"    repo_id={repr(repo_id)},\n"
            f"    private={private},\n"
            f"    license={repr(license_id)},\n"
            f"    upload_gguf={upload_gguf},\n"
            f"    upload_lora={upload_lora},\n"
            f"    upload_merged={upload_merged},\n"
            f"    upload_dataset={upload_dataset},\n"
            f"    base_model={repr(base_model)},\n"
            f"    dataset_name={repr(dataset_name)},\n"
            f"    lora_r={lora_r},\n"
            f"    lora_alpha={lora_alpha},\n"
            f"    lora_dropout={lora_dropout},\n"
            f"    num_epochs={num_epochs},\n"
            f"    learning_rate={learning_rate},\n"
            f"    max_seq_length={max_seq_length},\n"
            f"    batch_size={batch_size},\n"
            f"    gradient_accumulation={gradient_accumulation},\n"
            f"    optimizer={repr(optimizer)},\n"
            f"    lr_scheduler={repr(lr_scheduler)},\n"
            f")\n"
            f"\n"
            f"ok = upload(cfg, {repr(out_abs)})\n"
            f"if ok:\n"
            f'    print("PIPELINE_STAGE_COMPLETE=upload")\n'
            f"else:\n"
            f"    sys.exit(1)\n"
        )
        return script
