"""Qwen MTP inventory checks and BF16 conversion receipts.

Some checkpoints retain MTP configuration while omitting all draft weights.
Only a complete local safetensors inventory licenses exporting without MTP.
"""

import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import uuid

try:
    from markers import source_fingerprint
except ImportError:
    from .markers import source_fingerprint


_QWEN_ARCHITECTURES = {
    "Qwen3_5ForConditionalGeneration", "Qwen3_5ForCausalLM",
    "Qwen3_5MoeForConditionalGeneration", "Qwen3_5MoeForCausalLM",
    "Qwen3NextForCausalLM",
}


def _checkpoint_names(source: Path) -> set[str]:
    index_path = source / "model.safetensors.index.json"
    shards = sorted(source.glob("*.safetensors"))
    if not shards or (len(shards) > 1 and not index_path.exists()):
        raise ValueError("missing safetensors weights or shard index")
    weight_map = None
    if index_path.exists():
        weight_map = json.loads(index_path.read_text()).get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            raise ValueError("missing or invalid safetensors weight_map")
        if (not all(isinstance(k, str) and isinstance(v, str)
                    for k, v in weight_map.items())
                or set(weight_map.values()) != {p.name for p in shards}):
            raise ValueError("safetensors shard inventory does not match the index")

    inventory = {}
    for shard in shards:
        with shard.open("rb") as handle:
            prefix = handle.read(8)
            if len(prefix) != 8:
                raise ValueError(f"truncated safetensors header: {shard.name}")
            length = struct.unpack("<Q", prefix)[0]
            if not 2 <= length <= min(64 * 1024**2, shard.stat().st_size - 8):
                raise ValueError(f"invalid safetensors header size: {shard.name}")
            header = json.loads(handle.read(length))
        if not isinstance(header, dict):
            raise ValueError(f"invalid safetensors header: {shard.name}")
        payload_size = shard.stat().st_size - 8 - length
        for name, tensor in header.items():
            if name == "__metadata__":
                continue
            if name in inventory or not isinstance(tensor, dict):
                raise ValueError(f"duplicate or invalid tensor: {name}")
            offsets = tensor.get("data_offsets")
            if (not isinstance(offsets, list) or len(offsets) != 2
                    or not all(type(x) is int for x in offsets)
                    or not 0 <= offsets[0] < offsets[1] <= payload_size):
                raise ValueError(f"missing or truncated tensor payload: {name}")
            inventory[name] = shard.name
    if not inventory or (weight_map is not None and inventory != weight_map):
        raise ValueError("safetensors tensor inventory does not match the index")
    return set(inventory)


def bf16_conversion_policy(source: str | Path) -> dict | None:
    """Return a checked Qwen MTP policy, or None outside this narrow case."""
    source = Path(source)
    config_path = source / "config.json"
    if not config_path.is_file():
        return None
    config = json.loads(config_path.read_text())
    if not set(config.get("architectures", [])) & _QWEN_ARCHITECTURES:
        return None
    text = config.get("text_config", config)
    declared = text.get("mtp_num_hidden_layers", 0)
    if not declared:
        return None
    try:
        layers = text.get("num_hidden_layers")
        if (type(declared) is not int or not 1 <= declared <= 1024
                or type(layers) is not int or not 1 <= layers <= 100_000):
            raise ValueError("invalid Qwen layer counts")
        names = _checkpoint_names(source)
        trunk = {
            int(match[1]) for name in names
            if (match := re.match(r"(?:model\.)?(?:language_model\.)?layers\.(\d+)\.", name))
        }
        if trunk != set(range(layers)):
            raise ValueError("main-layer inventory is incomplete or ambiguous")
        mtp = {name for name in names if {"mtp", "nextn"} & set(name.split("."))}
        if mtp:
            mtp_layers = {
                int(match[1]) for name in mtp
                if (match := re.search(r"(?:^|\.)mtp\.layers\.(\d+)\.", name))
            }
            if mtp_layers != set(range(declared)):
                raise ValueError("declared MTP layers have incomplete or unrecognized weights")
            for layer in mtp_layers:
                if not any(name.endswith(f"mtp.layers.{layer}.input_layernorm.weight") for name in mtp):
                    raise ValueError(f"MTP layer {layer} is missing its input norm")
        return {"flags": [] if mtp else ["--no-mtp"],
                "source": source_fingerprint(source)}
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Cannot establish Qwen MTP inventory for {source}: {exc}") from exc


def _receipt_path(cached: Path) -> Path:
    return cached.with_name(cached.name + ".conversion.json")


def _artifact_state(path: Path) -> dict:
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _receipt(cached: Path, policy: dict) -> dict:
    return {"version": 1, "policy": policy, "artifact": _artifact_state(cached)}


def cached_bf16_matches(cached: Path, policy: dict | None) -> bool:
    if not cached.is_file():
        return False
    if policy is None:
        return True
    try:
        return json.loads(_receipt_path(cached).read_text()) == _receipt(cached, policy)
    except (OSError, ValueError):
        return False


def _validate_output(path: Path) -> None:
    try:
        from _rocmfpx_entry import _validate_quantized_gguf
    except ImportError:
        from ._rocmfpx_entry import _validate_quantized_gguf
    _validate_quantized_gguf(path)


def record_bf16_conversion(cached: Path, policy: dict) -> None:
    """Record an externally completed conversion after its caller verifies flags.

    The caller must have run the policy's exact flags and checked the model's
    MTP metadata. This helper checks structure and records local file identity.
    """
    _validate_output(cached)
    receipt = _receipt_path(cached)
    pending = receipt.with_name(f".{receipt.name}.{uuid.uuid4().hex}.partial")
    created = False
    try:
        with pending.open("x") as handle:
            created = True
            json.dump(_receipt(cached, policy), handle, indent=2)
        pending.replace(receipt)
    finally:
        if created:
            pending.unlink(missing_ok=True)


def run_bf16_conversion(argv: list[str], cached: Path, policy: dict | None) -> None:
    """Stage conversion and publish its receipt with rollback on commit failure."""
    nonce = uuid.uuid4().hex
    pending = cached.with_name(f".{cached.name}.{nonce}.partial")
    receipt = _receipt_path(cached)
    pending_receipt = receipt.with_name(f".{receipt.name}.{nonce}.partial")
    backup = cached.with_name(f".{cached.name}.{nonce}.previous")
    preserve_backup = False
    owned = set()
    try:
        with pending.open("xb"):
            owned.add(pending)
        command = list(argv)
        command[command.index("--outfile") + 1] = str(pending)
        command += policy["flags"] if policy else []
        if policy and policy["flags"]:
            print("Qwen config declares MTP but the complete checkpoint has no MTP "
                  "weights; converting the main model with --no-mtp", flush=True)
        rc = subprocess.run(command).returncode
        if rc != 0:
            raise RuntimeError(f"convert_hf_to_gguf.py failed (exit code {rc})")
        _validate_output(pending)
        if policy is not None:
            with pending_receipt.open("x") as handle:
                owned.add(pending_receipt)
                json.dump(_receipt(pending, policy), handle, indent=2)
        if cached.exists():
            pending.chmod(stat.S_IMODE(cached.stat().st_mode))
            os.link(cached, backup)
            owned.add(backup)
        pending.replace(cached)
        try:
            if policy is not None:
                pending_receipt.replace(receipt)
        except OSError:
            preserve_backup = True
            if backup.exists():
                backup.replace(cached)
            else:
                cached.unlink()
            preserve_backup = False
            raise
    finally:
        for path in owned:
            if path != backup or not preserve_backup:
                path.unlink(missing_ok=True)
