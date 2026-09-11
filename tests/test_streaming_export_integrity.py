"""CPU export parity using real local safetensors and PEFT adapter files."""

import copy
import json

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("peft")
from peft import LoraConfig, get_peft_model
from safetensors.torch import load_file, save_file

import fast_export as fe


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    monkeypatch.setattr(fe, "DEVICE", torch.device("cpu"))
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)


class Toy(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.q_proj = torch.nn.Linear(8, 8, bias=False)
        self.down_proj = torch.nn.Linear(8, 8, bias=False)

    def forward(self, x):
        return self.down_proj(self.q_proj(x))


@pytest.mark.parametrize("rslora", [False, True])
def test_streaming_merge_matches_peft_with_per_module_patterns(tmp_path, monkeypatch, rslora):
    import huggingface_hub

    def no_download(*args, **kwargs):
        pytest.fail("Local export attempted a Hub download")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", no_download)
    base = Toy()
    model_dir = tmp_path / "base"
    model_dir.mkdir()
    save_file(base.state_dict(), str(model_dir / "model.safetensors"))
    (model_dir / "config.json").write_text("{}")
    (model_dir / "tokenizer.model").write_bytes(b"sentencepiece fixture")
    cfg = LoraConfig(r=2, lora_alpha=4, target_modules=["q_proj", "down_proj"],
                     rank_pattern={"q_proj": 4}, alpha_pattern={"q_proj": 12}, use_rslora=rslora)
    adapted = get_peft_model(copy.deepcopy(base), cfg)
    with torch.no_grad():
        for name, parameter in adapted.named_parameters():
            if "lora_" in name:
                parameter.copy_(torch.randn_like(parameter))
    lora_dir = tmp_path / "adapters"
    adapted.save_pretrained(lora_dir)
    reference = adapted.merge_and_unload().state_dict()
    merged_dir = tmp_path / "merged"
    fe.streaming_merge(str(model_dir), str(lora_dir), str(merged_dir))
    actual = load_file(str(merged_dir / "model.safetensors"))
    for name in reference:
        torch.testing.assert_close(actual[name], reference[name])
    assert (merged_dir / "tokenizer.model").read_bytes() == b"sentencepiece fixture"


@pytest.mark.parametrize("option,value", [
    ("use_dora", True), ("fan_in_fan_out", True), ("bias", "all"),
    ("modules_to_save", ["lm_head"]), ("lora_bias", True),
])
def test_unsupported_peft_options_fail_closed(option, value):
    cfg = {"r": 2, "lora_alpha": 4, option: value}
    with pytest.raises(ValueError, match="does not support"):
        fe.build_lora_map(cfg, {})


@pytest.mark.parametrize("weights", [
    {"base_model.model.q_proj.lora_A.weight": torch.ones(2, 8)},
    {"base_model.model.q_proj.lora_B.weight": torch.ones(8, 2)},
    {"base_model.model.q_proj.bias": torch.ones(8)},
    {},
])
def test_incomplete_or_extra_adapter_tensors_fail_closed(weights):
    with pytest.raises(ValueError):
        fe.build_lora_map({"r": 2, "lora_alpha": 4}, weights)


def test_unmatched_adapter_target_rejected_before_writing_model(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    save_file({"different.weight": torch.zeros(8, 8)}, str(base / "model.safetensors"))
    lora = tmp_path / "lora"
    lora.mkdir()
    (lora / "adapter_config.json").write_text(json.dumps({"r": 2, "lora_alpha": 4, "target_modules": ["q_proj"]}))
    save_file({"base_model.model.q_proj.lora_A.weight": torch.ones(2, 8),
               "base_model.model.q_proj.lora_B.weight": torch.ones(8, 2)},
              str(lora / "adapter_model.safetensors"))
    out = tmp_path / "merged"
    with pytest.raises(ValueError, match="absent from base checkpoint"):
        fe.streaming_merge(str(base), str(lora), str(out))
    assert not out.exists()


def test_relative_local_gguf_is_resolved_without_hub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "model-bf16.gguf").write_bytes(b"GGUF")
    assert fe.resolve_gguf_source("model-bf16.gguf") == str(tmp_path / "model-bf16.gguf")


def test_base_directory_cannot_be_overwritten(tmp_path):
    key = tmp_path / "model.safetensors"
    save_file({"weight": torch.zeros(2, 2)}, str(key))
    before = key.read_bytes()
    with pytest.raises(ValueError, match="must differ"):
        fe.streaming_merge(str(tmp_path), None, str(tmp_path))
    assert key.read_bytes() == before


def test_gguf_passthrough_never_unlinks_its_own_source(tmp_path):
    source = tmp_path / "model-bf16.gguf"
    source.write_bytes(b"original GGUF weights")
    fe.streaming_merge(str(source), None, str(tmp_path / "merged_model"))
    assert not source.is_symlink()
    assert source.read_bytes() == b"original GGUF weights"


def test_hub_style_cached_shard_symlinks_are_supported(tmp_path):
    blob = tmp_path / "blob"
    save_file({"weight": torch.zeros(2, 2)}, str(blob))
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "model-00001.safetensors").symlink_to(blob)
    (snapshot / "model.safetensors.index.json").write_text(json.dumps({
        "weight_map": {"weight": "model-00001.safetensors"},
    }))
    out = tmp_path / "merged"
    fe.streaming_merge(str(snapshot), None, str(out))
    torch.testing.assert_close(load_file(str(out / "model-00001.safetensors"))["weight"], torch.zeros(2, 2))
