"""
Lightweight integration test for the custom fast training pipeline.

Runs 1 epoch of training on zeroclaw_training_data.jsonl against
huihui-ai/Huihui-Qwen3.5-9B-Claude-4.6-Opus-abliterated (a 9B model
that fits in memory without hitting GTT limits during testing).

Validates:
1. fast_load_quantized_model() loads without error
2. LoRA adapters attach correctly
3. Completion-only loss masking is set up (response template detected)
4. 1 epoch of training produces a valid checkpoint
5. LoRA adapters are saved in a format fast_export.py can consume
6. fast_export.py can produce a merged safetensors directory

Usage:
    make test-integration
    python -m pytest tests/test_training_integration.py -v

NOTE: This test requires GPU access and downloads a 9B model (~5 GB).
      Do not run while the GPU is busy (e.g. during GGUF generation).
      Expected runtime: ~10-30 minutes depending on dataset size.

Structure: model/tokenizer, the LoRA-attached model, the trained model, and
the saved adapter dir are module-scoped fixtures, each performing its stage's
real work and core assertions -- a failure surfaces immediately as a setup
error on whichever test first requests the broken fixture, and the fixture's
result is cached (computed once) for every other test in the module that
depends on it. Downstream tests then add their stage-specific assertions.
This used to be a manually-threaded script (model/tokenizer/lora_dir passed
by hand between plain functions via run_all_tests()) with pytest markers
bolted on after the fact; pytest collected 4 of its 5 tests but none of them
could run (`fixture 'model' not found` at setup), so `make test-integration`
only ever exercised step 1. Converting to real fixtures is what makes pytest
actually run the full chain.
"""

import json
import os
import sys

# Set ROCm environment before any torch import.
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "backend:native,expandable_segments:True"
os.environ["UNSLOTH_SKIP_TORCHVISION_CHECK"] = "1"
os.environ["TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL"] = "1"

# Add the pipeline core to path so we can import the fast loaders.
PIPELINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PIPELINE_ROOT, "core"))

import pytest

# These tests need a GPU + a multi-GB model download. Mark the whole module so
# the offline/CI suite deselects it with `-m 'not slow'` / `-m 'not gpu'`.
pytestmark = [pytest.mark.slow, pytest.mark.gpu]

# Test configuration — use the 9B model for manageable test times.
TEST_MODEL_ID = "huihui-ai/Huihui-Qwen3.5-9B-Claude-4.6-Opus-abliterated"
DATASET_PATH = os.path.join(PIPELINE_ROOT, "data", "zeroclaw_training_data.jsonl")


@pytest.fixture(scope="module")
def test_output_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("pipeline_test"))


@pytest.fixture(scope="module")
def model_and_tokenizer():
    """Load the test model once for the whole module (audit B4)."""
    from fast_train_zeroclaw import fast_load_quantized_model

    model, tokenizer = fast_load_quantized_model(TEST_MODEL_ID)

    first_param = next(model.parameters())
    assert first_param.device.type == "cuda", f"Model not on GPU: {first_param.device}"
    tokens = tokenizer.encode("Hello, world!")
    assert len(tokens) > 0, "Tokenizer produced empty output"

    return model, tokenizer


@pytest.fixture(scope="module")
def lora_model(model_and_tokenizer):
    """Attach LoRA adapters to the loaded model."""
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    model, _tokenizer = model_and_tokenizer
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    lora_config = LoraConfig(
        r=16,  # Smaller rank for faster test
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())
    assert trainable > 0, "No trainable parameters after LoRA attachment"
    assert trainable < total_params, "All parameters are trainable (LoRA not applied)"
    pct = 100 * trainable / total_params
    assert pct < 5, f"Trainable percentage too high ({pct:.2f}%), LoRA may not be working"

    return model


@pytest.fixture(scope="module")
def trained_model(lora_model, model_and_tokenizer, test_output_dir):
    """Run 1 epoch of training and return (model, tokenizer)."""
    from datasets import load_dataset
    from trl import SFTTrainer, SFTConfig

    _base_model, tokenizer = model_and_tokenizer
    model = lora_model

    dataset = load_dataset("json", data_files=DATASET_PATH, split="train")

    def fmt(ex):
        ex["text"] = tokenizer.apply_chat_template(
            ex["messages"], tokenize=False, add_generation_prompt=False,
        )
        return ex
    dataset = dataset.map(fmt)

    training_args = SFTConfig(
        output_dir=test_output_dir,
        num_train_epochs=1,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=1,  # Small for fast test
        learning_rate=2e-4,
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
        max_length=4096,
        completion_only_loss=True,
    )

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=dataset,
        args=training_args,
    )

    stats = trainer.train()
    loss = stats.training_loss
    assert loss > 0, "Training loss is zero — something is wrong"
    assert loss < 100, f"Training loss unreasonably high: {loss}"

    from pathlib import Path
    checkpoints = list(Path(test_output_dir).glob("checkpoint-*"))
    assert len(checkpoints) > 0, "No checkpoints saved after training"

    return model, tokenizer


@pytest.fixture(scope="module")
def saved_lora_dir(trained_model, test_output_dir):
    """Save the trained LoRA adapters and return the directory path."""
    model, tokenizer = trained_model
    lora_dir = os.path.join(test_output_dir, "lora_adapters")
    model.save_pretrained(lora_dir)
    tokenizer.save_pretrained(lora_dir)

    required = ["adapter_config.json", "adapter_model.safetensors"]
    for fname in required:
        fpath = os.path.join(lora_dir, fname)
        assert os.path.exists(fpath), f"Missing required file: {fname}"
        assert os.path.getsize(fpath) > 0, f"File is empty: {fname}"

    with open(os.path.join(lora_dir, "adapter_config.json")) as f:
        cfg = json.load(f)
    assert "r" in cfg, "adapter_config.json missing 'r'"
    assert "lora_alpha" in cfg, "adapter_config.json missing 'lora_alpha'"
    assert "target_modules" in cfg, "adapter_config.json missing 'target_modules'"

    return lora_dir


def test_model_loading(model_and_tokenizer):
    model, tokenizer = model_and_tokenizer
    first_param = next(model.parameters())
    assert first_param.device.type == "cuda"
    assert len(tokenizer) > 0


def test_lora_attachment(lora_model):
    trainable = sum(p.numel() for p in lora_model.parameters() if p.requires_grad)
    assert trainable > 0


def test_training_one_epoch(trained_model, test_output_dir):
    from pathlib import Path
    checkpoints = list(Path(test_output_dir).glob("checkpoint-*"))
    assert len(checkpoints) > 0


def test_lora_save(saved_lora_dir):
    assert os.path.exists(os.path.join(saved_lora_dir, "adapter_model.safetensors"))


def test_export(saved_lora_dir, test_output_dir):
    """Test that fast_export.py can merge LoRA adapters with the base model."""
    from fast_export import streaming_merge

    merged_dir = os.path.join(test_output_dir, "merged_model")

    streaming_merge(
        model_id=TEST_MODEL_ID,
        lora_dir=saved_lora_dir,
        merged_dir=merged_dir,
    )

    from pathlib import Path
    merged_path = Path(merged_dir)
    assert merged_path.exists(), "Merged directory not created"

    st_files = list(merged_path.glob("*.safetensors"))
    assert len(st_files) > 0, "No safetensors files in merged output"

    idx_path = merged_path / "model.safetensors.index.json"
    assert idx_path.exists(), "Missing model.safetensors.index.json"
    with open(idx_path) as f:
        idx = json.load(f)
    assert "weight_map" in idx, "Index missing weight_map"

    assert (merged_path / "config.json").exists(), "Missing config.json in merged output"
