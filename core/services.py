"""
Service layer for Foundry pipeline stages.

Extracts business logic from ui/app.py route handlers into testable,
reusable service classes. Each service accepts a config and a progress
callback, handles subprocess execution, and returns success/failure.

Usage from the UI:
    svc = TrainingService(project_root, venv_python)
    ok = await svc.run(cfg, run_script_fn)
"""

import json
import os
from pathlib import Path
from typing import Callable, Awaitable, Optional

# Stages whose body has been extracted into an importable core/_<stage>_entry.py
# module (audit H2). The Service.build_script for these emits a thin shim that
# writes a JSON config and invokes the entry module's run().
ENTRY_MODULES = ("_train_entry",)

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


def _entry_shim(entry_module: str, cfg: dict, pipeline_root: Path) -> str:
    """Return a small shim that writes ``cfg`` as JSON and calls an entry module.

    The shim is intentionally tiny and config-driven: the CLI and the UI produce
    a byte-identical shim for the same module, and all stage logic lives in the
    importable ``core/<entry_module>.py`` (audit H2). The JSON config is embedded
    as a repr'd string and written next to the running script, then passed to
    ``run()``.
    """
    core_path = repr(str(Path(pipeline_root) / "core"))
    cfg_json = repr(json.dumps(cfg, indent=2, sort_keys=True))
    return (
        "import sys, json, os\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {core_path})\n"
        f"_cfg_path = Path(__file__).with_name({repr(entry_module + '.cfg.json')})\n"
        f"_cfg_path.write_text({cfg_json})\n"
        f"import {entry_module}\n"
        f"{entry_module}.run(str(_cfg_path))\n"
    )


class TrainingService:
    """Orchestrates the QLoRA training subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model_name: str,
        datasets: list[str] = None,
        dataset_path: str = "",
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
        warmup_steps: Optional[int] = None,
        warmup_ratio: Optional[float] = None,
        optim: str,
        packing: bool = False,
    ) -> dict:
        """Build the JSON-serializable config consumed by core/_train_entry.py.

        Warmup: pass ``warmup_ratio`` (preferred, used by both CLI and UI) and/or
        ``warmup_steps``. When ``warmup_ratio`` is set it takes precedence and
        ``warmup_steps`` is dropped; at least one must be provided (audit
        M-warmup). The same config from CLI and UI yields the same entry-module
        invocation, so produced adapters match.
        """
        if warmup_ratio is None and warmup_steps is None:
            raise ValueError("TrainingService.build_config requires warmup_ratio or warmup_steps")
        sources = datasets if datasets else ([dataset_path] if dataset_path else None)
        return {
            "pipeline_root": str(self.pipeline_root),
            "model_name": model_name,
            "datasets": list(sources) if sources else None,
            "output_dir": output_dir,
            "max_seq_length": max_seq_length,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "lora_dropout": lora_dropout,
            "use_rslora": use_rslora,
            "num_train_epochs": num_train_epochs,
            "per_device_train_batch_size": per_device_train_batch_size,
            "gradient_accumulation_steps": gradient_accumulation_steps,
            "learning_rate": learning_rate,
            "lr_scheduler_type": lr_scheduler_type,
            # Ratio wins: drop warmup_steps entirely when a ratio is given.
            "warmup_ratio": warmup_ratio,
            "warmup_steps": None if warmup_ratio is not None else warmup_steps,
            "optim": optim,
            "packing": packing,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the training subprocess shim.

        The shim writes the JSON config (built by :meth:`build_config`) next to
        itself and invokes the importable entry module ``core/_train_entry.py``.
        All real logic — dataset format normalization, kbit prep, LoRA setup,
        SFTConfig construction — lives in that module so it is IDE/lint/type
        friendly and unit-testable without a GPU (audit H2).
        """
        cfg = self.build_config(**kwargs)
        return _entry_shim("_train_entry", cfg, self.pipeline_root)


class ExportService:
    """Orchestrates the streaming LoRA merge / export subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        base_model_id: str,
        lora_source: Optional[str],
        has_lora: bool,
        merged_dir: str,
    ) -> dict:
        """Build the JSON config consumed by core/_export_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "base_model_id": base_model_id,
            "lora_source": lora_source,
            "has_lora": has_lora,
            "merged_dir": merged_dir,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the export subprocess shim (calls core/_export_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_export_entry", cfg, self.pipeline_root)


class HereticService:
    """Orchestrates the heretic abliteration subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model_path: str,
        output_path: str,
        checkpoint_dir: str,
        n_trials: int,
        n_startup_trials: int,
        quantization: str,
        kl_divergence_scale: float,
        orthogonalize_direction: bool,
        row_normalization: str,
    ) -> dict:
        """Build the JSON config consumed by core/_heretic_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "model_path": model_path,
            "output_path": output_path,
            "checkpoint_dir": checkpoint_dir,
            "n_trials": n_trials,
            "n_startup_trials": n_startup_trials,
            "quantization": quantization,
            "kl_divergence_scale": kl_divergence_scale,
            "orthogonalize_direction": orthogonalize_direction,
            "row_normalization": row_normalization,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the heretic subprocess shim (calls core/_heretic_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_heretic_entry", cfg, self.pipeline_root)



class ReapService:
    """Orchestrates the REAP expert pruning subprocess.

    REAP (Router-weighted Expert Activation Pruning) prunes experts from MoE
    models. Its own pyproject.toml pins torch==2.7.1 / transformers==4.55.0 /
    vllm==0.10.0, which would break Foundry's ROCm stack. So we import
    ``reap.prune`` by adding its ``src/`` to ``sys.path`` and stubbing every
    heavy optional dependency (vllm, lm_eval, evalplus, lcb_runner, crfm_helm,
    evalscope, uvloop, deepspeed, wandb) at subprocess startup.

    REAP writes its output to a path like
    ``artifacts/<model>/<dataset>/pruned_models/<method>-seed_<seed>-<ratio>/``
    relative to cwd. We chdir into the Foundry output directory so that
    relative path lands inside the run, then move the pruned model into
    ``<output_dir>/reap_model/``.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        input_dir: str,     # heretic_model or merged_model path (absolute)
        output_dir: str,    # absolute path where reap_model should end up
        cwd_dir: str,       # working directory for REAP's relative artifact paths
        compression_ratio: float,
        prune_method: str,
        samples_per_category: int,
        model_max_length: int,
        dataset_name: str,
        seed: int,
    ) -> dict:
        """Build the JSON config consumed by core/_reap_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "input_dir": input_dir,
            "output_dir": output_dir,
            "cwd_dir": cwd_dir,
            "compression_ratio": compression_ratio,
            "prune_method": prune_method,
            "samples_per_category": samples_per_category,
            "model_max_length": model_max_length,
            "dataset_name": dataset_name,
            "seed": seed,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the REAP subprocess shim (calls core/_reap_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_reap_entry", cfg, self.pipeline_root)



class MagicQuantService:
    """Orchestrates the MagicQuant evolutionary search subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
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
        verify: bool = False,
        measured: bool = False,
        measurement_rounds: int = 3,
        rocmfpx_schemes: bool = False,
        iq_schemes: bool = False,
        seed: Optional[int] = None,
        use_imatrix: bool = False,
        imatrix_corpus: Optional[str] = None,
        enable_kl: bool = False,
        kl_weight: float = 0.1,
        enable_speed_bench: bool = False,
        measurement_chunks: Optional[int] = None,
        stream_aware: bool = False,
        head_aggressive: bool = False,
    ) -> dict:
        """Build the JSON config consumed by core/_magicquant_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "llamacpp_hint": llamacpp_hint,
            "pipeline_root_str": pipeline_root_str,
            "mq_source_override": mq_source_override,
            "out_abs_str": out_abs_str,
            "generations": generations,
            "population_size": population_size,
            "target_base_quant": target_base_quant,
            "tiers_json": tiers_json,
            "model_name": model_name,
            "verify": verify,
            "measured": measured,
            "measurement_rounds": measurement_rounds,
            "rocmfpx_schemes": rocmfpx_schemes,
            "iq_schemes": iq_schemes,
            "seed": seed,
            "use_imatrix": use_imatrix,
            "imatrix_corpus": imatrix_corpus,
            "enable_kl": enable_kl,
            "kl_weight": kl_weight,
            "enable_speed_bench": enable_speed_bench,
            "measurement_chunks": measurement_chunks,
            "stream_aware": stream_aware,
            "head_aggressive": head_aggressive,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the MagicQuant subprocess shim (calls _magicquant_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_magicquant_entry", cfg, self.pipeline_root)


class QATService:
    """Orchestrates the QAT-LoRA subprocess.

    Quantization-Aware Training fine-tunes LoRA adapters that are robust to
    MagicQuant's per-group hybrid quant config (read from a prior search's
    ``search_results.json``). The heavy work lives in MagicQuant's
    ``magicquant.qat.run_qat``; this service builds the JSON config that the
    importable ``core/_qat_entry.py`` consumes — one source of truth for CLI + UI.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        model: str,
        config_path: str,   # path to the prior search's search_results.json
        tier: str,
        dataset: str,
        out: str,
        lora_r: int,
        lora_alpha: float,
        epochs: float,
        max_steps: int,
        lr: float,
        max_seq_len: int,
    ) -> dict:
        """Build the JSON config consumed by core/_qat_entry.py.

        The keys mirror ``magicquant.qat.run_qat``'s contract exactly (model,
        config + tier, dataset, out, and the LoRA/training hyperparams); the entry
        module strips ``pipeline_root`` before dispatch.
        """
        return {
            "pipeline_root": str(self.pipeline_root),
            "model": model,
            "config": config_path,
            "tier": tier,
            "dataset": dataset,
            "out": out,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "epochs": epochs,
            "max_steps": max_steps,
            "lr": lr,
            "max_seq_len": max_seq_len,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the QAT subprocess shim (calls core/_qat_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_qat_entry", cfg, self.pipeline_root)


class ROCmFPXService:
    """Orchestrates the ROCmFPX (AMD-tuned GGUF quant family) subprocess.

    ROCmFPX (https://github.com/ciru-ai/ROCmFPX) is a llama.cpp fork adding
    the ROCmFP3/4/6/8 quant types; it is a native build (git clone + compile),
    not a pip package. The heavy work (discovery/auto-install, BF16 GGUF
    conversion, per-format quantize) lives in the importable
    ``core/_rocmfpx_entry.py`` -- this service builds the JSON config that
    module consumes, the same shape as ``MagicQuantService``.
    """

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
        self,
        *,
        rocmfpx_hint: str,
        pipeline_root_str: str,
        source_override: str,
        out_abs_str: str,
        formats_json: str,
        model_name: str,
        imatrix: str = "",
    ) -> dict:
        """Build the JSON config consumed by core/_rocmfpx_entry.py."""
        return {
            "pipeline_root": str(self.pipeline_root),
            "rocmfpx_hint": rocmfpx_hint,
            "pipeline_root_str": pipeline_root_str,
            "source_override": source_override,
            "out_abs_str": out_abs_str,
            "formats_json": formats_json,
            "model_name": model_name,
            "imatrix": imatrix,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the ROCmFPX subprocess shim (calls _rocmfpx_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_rocmfpx_entry", cfg, self.pipeline_root)


class UploadService:
    """Orchestrates the HuggingFace upload subprocess."""

    def __init__(self, pipeline_root: Path, venv_python: str) -> None:
        self.pipeline_root = pipeline_root
        self.venv_python = venv_python

    def build_config(
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
        did_training: bool = True,
        did_heretic: bool = False,
        did_reap: bool = False,
        did_magicquant: bool = True,
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
    ) -> dict:
        """Build the JSON config consumed by core/_upload_entry.py.

        ``license_id`` maps to the HFUploadConfig ``license`` field.
        """
        return {
            "pipeline_root": str(self.pipeline_root),
            "repo_id": repo_id,
            "private": private,
            "license": license_id,
            "upload_gguf": upload_gguf,
            "upload_lora": upload_lora,
            "upload_merged": upload_merged,
            "upload_dataset": upload_dataset,
            "base_model": base_model,
            "dataset_name": dataset_name,
            "did_training": did_training,
            "did_heretic": did_heretic,
            "did_reap": did_reap,
            "did_magicquant": did_magicquant,
            "lora_r": lora_r,
            "lora_alpha": lora_alpha,
            "lora_dropout": lora_dropout,
            "num_epochs": num_epochs,
            "learning_rate": learning_rate,
            "max_seq_length": max_seq_length,
            "batch_size": batch_size,
            "gradient_accumulation": gradient_accumulation,
            "optimizer": optimizer,
            "lr_scheduler": lr_scheduler,
            "out_abs": out_abs,
        }

    def build_script(self, **kwargs) -> str:
        """Generate the upload subprocess shim (calls core/_upload_entry.py)."""
        cfg = self.build_config(**kwargs)
        return _entry_shim("_upload_entry", cfg, self.pipeline_root)
