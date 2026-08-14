#!/usr/bin/env python3
"""
Patch GGUF files to add missing chat template and token IDs.
Rewrites the file with additional KV metadata without touching tensor data.
"""

import struct
import sys
import os
from pathlib import Path


def read_string(f):
    length = struct.unpack('<Q', f.read(8))[0]
    return f.read(length).decode('utf-8')


def write_string(f, s):
    encoded = s.encode('utf-8')
    f.write(struct.pack('<Q', len(encoded)))
    f.write(encoded)


def read_value(f, vtype):
    """Read a GGUF value and return (value, raw_bytes).

    NOTE: the numeric ``vtype`` literals below (8=string, 4=uint32, ...) are
    an independent, hand-typed copy of the same GGUF value-type tags
    MagicQuant's ``magicquant/gguf/writer.py`` (``_GGUF_TYPE_*``) derives
    from the installed ``gguf`` package's ``gguf.constants.GGUFValueType``
    enum. writer.py is the canonical source for these; this standalone
    script is deliberately not restructured to import from it (or from
    `gguf`) -- kept in sync by hand if the format ever changes.
    """
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


def _tensor_info_length(rest_data, n_tensors):
    """Byte length of the tensor-info section at the head of ``rest_data``.

    Needed because the tensor DATA section must begin at a file offset that is
    a multiple of ``general.alignment``. Growing the KV section shifts
    everything after it, so the original padding cannot simply be replayed --
    doing that leaves the data section misaligned and every tensor reads from
    the wrong offset (silently: the file still loads, the weights are garbage).

    Entry layout: name (u64 len + bytes), n_dims (u32), dims (n_dims x u64),
    ggml type (u32), offset (u64).
    """
    pos = 0
    for _ in range(n_tensors):
        (name_len,) = struct.unpack_from('<Q', rest_data, pos)
        pos += 8 + name_len
        (n_dims,) = struct.unpack_from('<I', rest_data, pos)
        pos += 4 + n_dims * 8
        pos += 4  # ggml type
        pos += 8  # offset
    return pos


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
        alignment = 32  # GGUF default when general.alignment is absent
        for i in range(n_kv):
            key = read_string(fin)
            vtype = struct.unpack('<I', fin.read(4))[0]
            val, raw = read_value(fin, vtype)
            kv_pairs.append((key, vtype, raw))
            existing_keys.add(key)
            if key == 'general.alignment':
                alignment = val

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

        # rest_data is: tensor_info entries + alignment padding + tensor data.
        #
        # Per-tensor offsets inside tensor_info are relative to the START of the
        # tensor data section, so they need no adjustment. What DOES move is the
        # data section itself: it must begin at a file offset that is a multiple
        # of general.alignment, and we just grew the KV section by an arbitrary
        # number of bytes. Replaying the original padding verbatim leaves the
        # data section off its boundary; the file still opens, and every tensor
        # then reads from the wrong place. Recompute the padding instead.
        info_len = _tensor_info_length(rest_data, n_tensors)
        orig_data_start = (rest_start + info_len + alignment - 1) // alignment * alignment
        fout.write(rest_data[:info_len])
        pad = -fout.tell() % alignment
        fout.write(b'\x00' * pad)
        fout.write(rest_data[orig_data_start - rest_start:])

    # Replace original
    os.replace(output_path, input_path)
    print(f"  Patched successfully!")


def find_gguf_files(dirs) -> list:
    """Collect *.gguf files across one or more directories, sorted per directory.

    Non-existent directories are skipped with a message, not an error --
    callers routinely pass a set of candidate locations where not all of
    them exist for a given run.
    """
    files = []
    for d in dirs:
        gguf_dir = Path(d)
        if not gguf_dir.exists():
            print(f"Skipping {gguf_dir} (does not exist)")
            continue
        files.extend(sorted(gguf_dir.glob("*.gguf")))
    return files


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Patch GGUF files with a missing chat template + EOS/pad token IDs, "
                     "sourced from a HuggingFace tokenizer.",
    )
    parser.add_argument(
        "--model-id", required=True,
        help="HF repo id (or local path) to load the tokenizer from, e.g. "
             "'org/model-name'.",
    )
    parser.add_argument(
        "--gguf-dir", required=True, nargs="+", dest="gguf_dirs",
        help="One or more directories to scan for *.gguf files (non-existent "
             "directories are skipped with a warning, not an error).",
    )
    args = parser.parse_args()

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.model_id, trust_remote_code=True)

    gguf_files = find_gguf_files(args.gguf_dirs)

    if not gguf_files:
        print("No GGUF files found!")
        sys.exit(1)

    print(f"Found {len(gguf_files)} GGUF files to patch\n")

    for gguf_path in gguf_files:
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
