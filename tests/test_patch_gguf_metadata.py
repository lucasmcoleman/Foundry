"""scripts/patch_gguf_metadata.py's CLI parameterization (audit B5).

main() used to hardcode two paths from before the project's rename from
/server/programming/pipeline to Foundry -- both gone, so running the script
as committed just printed "No GGUF files found!" and exited 1, even though
CLAUDE.md's Known Issues section points users here as the fix for GGUFs
missing a chat template. Now takes --model-id and one or more --gguf-dir
directories as CLI args instead.

find_gguf_files (the extracted directory-scan helper) is the only part
testable without a live HuggingFace tokenizer download; main()'s tokenizer
load + actual GGUF byte-patching are exercised by hand / in real runs, not
here.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from patch_gguf_metadata import find_gguf_files

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch_gguf_metadata.py"


def test_find_gguf_files_collects_across_multiple_dirs(tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.gguf").write_bytes(b"")
    (d1 / "b.gguf").write_bytes(b"")
    (d1 / "not-a-gguf.txt").write_bytes(b"")
    (d2 / "c.gguf").write_bytes(b"")

    found = find_gguf_files([str(d1), str(d2)])
    names = sorted(p.name for p in found)
    assert names == ["a.gguf", "b.gguf", "c.gguf"]


def test_find_gguf_files_skips_nonexistent_dir_without_raising(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "x.gguf").write_bytes(b"")

    found = find_gguf_files([str(tmp_path / "does-not-exist"), str(real_dir)])
    assert [p.name for p in found] == ["x.gguf"]


def test_find_gguf_files_empty_when_nothing_found(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert find_gguf_files([str(empty_dir)]) == []


def test_cli_requires_model_id_and_gguf_dir():
    # No network/tokenizer access needed: argparse rejects a missing
    # required arg before main() reaches AutoTokenizer.from_pretrained.
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "--model-id" in result.stderr
    assert "--gguf-dir" in result.stderr


def test_cli_help_documents_both_required_args():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--model-id" in result.stdout
    assert "--gguf-dir" in result.stdout
