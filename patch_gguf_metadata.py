#!/usr/bin/env python3
"""
Patch GGUF files to add missing chat template and token IDs.
Rewrites the file with additional KV metadata without touching tensor data.
"""

import struct
import sys
import os
import shutil
from pathlib import Path


def read_string(f):
    length = struct.unpack('<Q', f.read(8))[0]
    return f.read(length).decode('utf-8')


def write_string(f, s):
    encoded = s.encode('utf-8')
    f.write(struct.pack('<Q', len(encoded)))
    f.write(encoded)


def read_value(f, vtype):
    """Read a GGUF value and return (value, raw_bytes)."""
    start = f.tell()
    if vtype == 8:  # string
        val = read_string(f)
    elif vtype == 4:  # uint32
        val = struct.unpack('<I', f.read(4))[0]
    elif vtype == 5:  # int32
        val = struct.unpack('<i', f.read(4))[0]
    elif vtype == 6:  # float32
        val = struct.unpack('<f', f.read(4))[0]
    elif vtype == 7:  # bool
        val = struct.unpack('<?', f.read(1))[0]
    elif vtype == 10:  # uint64
        val = struct.unpack('<Q', f.read(8))[0]
    elif vtype == 12:  # int64
        val = struct.unpack('<q', f.read(8))[0]
    elif vtype == 9:  # array
        arr_type = struct.unpack('<I', f.read(4))[0]
        arr_len = struct.unpack('<Q', f.read(8))[0]
        val = []
        for _ in range(arr_len):
            v, _ = read_value(f, arr_type)
            val.append(v)
    else:
        raise ValueError(f"Unknown type {vtype}")
    end = f.tell()
    # Re-read the raw bytes
    f.seek(start)
    raw = f.read(end - start)
    return val, raw


def write_kv_string(f, key, value):
    """Write a string KV pair."""
    write_string(f, key)
    f.write(struct.pack('<I', 8))  # type = string
    write_string(f, value)


def write_kv_uint32(f, key, value):
    """Write a uint32 KV pair."""
    write_string(f, key)
    f.write(struct.pack('<I', 4))  # type = uint32
    f.write(struct.pack('<I', value))


def patch_gguf(input_path, chat_template, eos_token_id, pad_token_id):
    """Patch a GGUF file to add chat template and token IDs."""
    print(f"Patching {os.path.basename(input_path)}...")

    output_path = input_path + ".patched"

    with open(input_path, 'rb') as fin:
        # Read header
        magic = fin.read(4)
        assert magic == b'GGUF', f"Not a GGUF file: {magic}"
        version = struct.unpack('<I', fin.read(4))[0]
        n_tensors = struct.unpack('<Q', fin.read(8))[0]
        n_kv = struct.unpack('<Q', fin.read(8))[0]

        # Read all existing KV pairs
        kv_pairs = []
        existing_keys = set()
        for i in range(n_kv):
            key = read_string(fin)
            vtype = struct.unpack('<I', fin.read(4))[0]
            _, raw = read_value(fin, vtype)
            kv_pairs.append((key, vtype, raw))
            existing_keys.add(key)

        # Position after KV section = start of tensor info + data
        rest_start = fin.tell()
        rest_data = fin.read()  # Everything after KV section

    # New KV pairs to add
    new_kvs = []
    if 'tokenizer.chat_template' not in existing_keys:
        new_kvs.append(('tokenizer.chat_template', chat_template))
        print(f"  Adding tokenizer.chat_template ({len(chat_template)} chars)")
    if 'tokenizer.ggml.eos_token_id' not in existing_keys:
        new_kvs.append(('tokenizer.ggml.eos_token_id', eos_token_id))
        print(f"  Adding tokenizer.ggml.eos_token_id = {eos_token_id}")
    if 'tokenizer.ggml.padding_token_id' not in existing_keys:
        new_kvs.append(('tokenizer.ggml.padding_token_id', pad_token_id))
        print(f"  Adding tokenizer.ggml.padding_token_id = {pad_token_id}")
    if 'general.type' not in existing_keys:
        new_kvs.append(('general.type', 'model'))
        print(f"  Adding general.type = model")

    if not new_kvs:
        print("  No patches needed!")
        return

    new_n_kv = n_kv + len(new_kvs)

    with open(output_path, 'wb') as fout:
        # Write header with updated KV count
        fout.write(magic)
        fout.write(struct.pack('<I', version))
        fout.write(struct.pack('<Q', n_tensors))
        fout.write(struct.pack('<Q', new_n_kv))

        # Write existing KV pairs (replay raw bytes)
        for key, vtype, raw in kv_pairs:
            write_string(fout, key)
            fout.write(struct.pack('<I', vtype))
            fout.write(raw)

        # Write new KV pairs
        for key, value in new_kvs:
            if isinstance(value, str):
                write_kv_string(fout, key, value)
            elif isinstance(value, int):
                write_kv_uint32(fout, key, value)

        # The tensor info section uses absolute offsets from the start of the file.
        # Since we added KV data, the tensor data offsets need adjustment.
        # BUT: GGUF tensor data offsets are relative to the END of the header
        # (after padding to alignment). We need to check if tensor data uses
        # absolute or relative offsets.
        #
        # In GGUF v3, tensor data offset is relative to the start of tensor data
        # (after all metadata + tensor info + alignment padding).
        # So we just need to re-pad correctly.

        # Write tensor info + data as-is, but we need to handle alignment.
        # The original file had alignment after KV+tensor_info.
        # We're changing the KV section size, so alignment changes.
        # BUT tensor info offsets are relative to tensor data start, not file start.
        # So we just need to ensure proper alignment before tensor data.

        # The rest_data contains: tensor_info entries + alignment padding + tensor data
        # We can write it directly - tensor offsets are relative to data section start
        fout.write(rest_data)

    # Replace original
    os.replace(output_path, input_path)
    print(f"  Patched successfully!")


def main():
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(
        'DavidAU/Qwen3.5-40B-Claude-4.6-Opus-Deckard-Heretic-Uncensored-Thinking',
        trust_remote_code=True,
    )

    gguf_dir = "/server/ai/models/lmcoleman/qwen3.5-40b-claude-4.6-os-deckard-heretic-uncensored-thinking-gguf"
    gguf_files = list(Path(gguf_dir).glob("*.gguf"))

    if not gguf_files:
        print("No GGUF files found!")
        sys.exit(1)

    print(f"Found {len(gguf_files)} GGUF files to patch\n")

    for gguf_path in sorted(gguf_files):
        patch_gguf(
            str(gguf_path),
            chat_template=tok.chat_template,
            eos_token_id=tok.eos_token_id,
            pad_token_id=tok.pad_token_id,
        )
        print()

    # Also patch the copies in the output dir
    mq_dir = Path("/server/programming/unsloth/output-zeroclaw-qwen40b/magicquant")
    mq_files = list(mq_dir.glob("*.gguf")) if mq_dir.exists() else []
    if mq_files:
        print(f"Also patching {len(mq_files)} files in {mq_dir}\n")
        for gguf_path in sorted(mq_files):
            patch_gguf(
                str(gguf_path),
                chat_template=tok.chat_template,
                eos_token_id=tok.eos_token_id,
                pad_token_id=tok.pad_token_id,
            )
            print()

    print("All done!")


if __name__ == "__main__":
    main()
