"""MTP-serving helpers (core/serving.py): "nextn" draft-tensor detection,
llama-server command construction, the hf_upload model-card MTP section, and
the read-only /api/serve-command UI endpoint.

magicquant is a sibling repo, not a hard Foundry dependency (see
core/_magicquant_entry.py's lazy `from magicquant...` imports) -- so
detect_mtp's GGUFSource is stubbed via sys.modules injection rather than
requiring a real install, mirroring test_gguf_source_routing.py's
huggingface_hub stub.
"""

import sys
import types

import pytest

import serving


@pytest.fixture
def stub_gguf_source(monkeypatch):
    """Register (path -> tensor_names) pairs and stub
    magicquant.gguf.source.GGUFSource to read from that registry. An
    unregistered path raises FileNotFoundError, like a real missing-file
    open would.

    Returns a callable: register(path, tensor_names).
    """
    registry: dict[str, list[str]] = {}

    class _StubGGUFSource:
        def __init__(self, path):
            if path not in registry:
                raise FileNotFoundError(path)
            self._names = registry[path]

        def get_tensor_names(self):
            return self._names

        def close(self):
            pass

    fake_source_mod = types.ModuleType("magicquant.gguf.source")
    fake_source_mod.GGUFSource = _StubGGUFSource
    monkeypatch.setitem(sys.modules, "magicquant", types.ModuleType("magicquant"))
    monkeypatch.setitem(sys.modules, "magicquant.gguf", types.ModuleType("magicquant.gguf"))
    monkeypatch.setitem(sys.modules, "magicquant.gguf.source", fake_source_mod)

    return registry.__setitem__


@pytest.fixture
def fixed_server_bin(monkeypatch):
    """Pin the resolved llama-server binary so command-shape assertions don't
    depend on whatever ROCmFPX/llama.cpp build happens to be installed on the
    box running the tests -- binary discovery itself is
    _rocmfpx_entry.find_rocmfpx's job (covered by test_rocmfpx_entry.py).
    """
    monkeypatch.setattr(serving, "_resolve_llama_server", lambda hint="": "llama-server")


# ── detect_mtp ────────────────────────────────────────────────────────────────

def test_detect_mtp_true_when_nextn_tensor_present(stub_gguf_source):
    stub_gguf_source("model.gguf", ["blk.0.attn_q.weight", "blk.46.nextn.eh_proj.weight"])
    assert serving.detect_mtp("model.gguf") is True


def test_detect_mtp_false_without_nextn_tensor(stub_gguf_source):
    stub_gguf_source("model.gguf", ["blk.0.attn_q.weight", "blk.0.attn_k.weight"])
    assert serving.detect_mtp("model.gguf") is False


def test_detect_mtp_false_for_missing_file(stub_gguf_source):
    # Never registered with the stub -- __init__ raises FileNotFoundError,
    # exactly like a real missing-file open would.
    assert serving.detect_mtp("does-not-exist.gguf") is False


# ── build_serve_command ────────────────────────────────────────────────────────

def test_build_serve_command_auto_enables_mtp_when_nextn_present(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", ["blk.46.nextn.eh_proj.weight"])
    argv = serving.build_serve_command("m.gguf")
    assert "-md" in argv
    assert argv[argv.index("-md") + 1] == "m.gguf"  # self-speculative: same file, no separate draft checkpoint
    assert "--spec-type" in argv
    assert argv[argv.index("--spec-type") + 1] == "draft-mtp"


def test_build_serve_command_no_mtp_without_nextn(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", ["blk.0.attn_q.weight"])
    argv = serving.build_serve_command("m.gguf")
    assert "-md" not in argv
    assert "--spec-type" not in argv


def test_build_serve_command_mtp_true_forces_on_even_without_nextn(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", ["blk.0.attn_q.weight"])
    argv = serving.build_serve_command("m.gguf", mtp=True)
    assert "-md" in argv
    assert argv[argv.index("-md") + 1] == "m.gguf"


def test_build_serve_command_mtp_false_disables_even_with_nextn(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", ["blk.46.nextn.eh_proj.weight"])
    argv = serving.build_serve_command("m.gguf", mtp=False)
    assert "-md" not in argv
    assert "--spec-type" not in argv


def test_build_serve_command_kv_and_flash_flags_default_on(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", [])
    argv = serving.build_serve_command("m.gguf")
    assert argv[argv.index("-ctk") + 1] == "q8_0"
    assert argv[argv.index("-ctv") + 1] == "q8_0"
    assert argv[argv.index("-fa") + 1] == "on"


def test_build_serve_command_kv_and_flash_flags_can_be_disabled(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", [])
    argv = serving.build_serve_command("m.gguf", kv_quant=False, flash_attn=False)
    assert "-ctk" not in argv
    assert "-ctv" not in argv
    assert "-fa" not in argv


def test_build_serve_command_uses_resolved_server_binary_and_gguf_path(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", [])
    argv = serving.build_serve_command("m.gguf", port=9001, ctx=4096, host="0.0.0.0", ngl=50)
    assert argv[0] == "llama-server"
    assert argv[argv.index("-m") + 1] == "m.gguf"
    assert argv[argv.index("-c") + 1] == "4096"
    assert argv[argv.index("--port") + 1] == "9001"
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("-ngl") + 1] == "50"


# ── format_serve_command ────────────────────────────────────────────────────────

def test_format_serve_command_quotes_paths_with_spaces():
    argv = ["llama-server", "-m", "/path with spaces/model.gguf", "--port", "8080"]
    assert serving.format_serve_command(argv) == \
        "llama-server -m '/path with spaces/model.gguf' --port 8080"


def test_format_serve_command_round_trips_build_serve_command(stub_gguf_source, fixed_server_bin):
    stub_gguf_source("m.gguf", ["blk.46.nextn.eh_proj.weight"])
    cmd = serving.format_serve_command(serving.build_serve_command("m.gguf"))
    assert cmd.startswith("llama-server ")
    assert "--spec-type draft-mtp" in cmd


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_main_notes_auto_enabled_mtp(stub_gguf_source, fixed_server_bin, capsys):
    stub_gguf_source("m.gguf", ["blk.46.nextn.eh_proj.weight"])
    rc = serving._main(["m.gguf", "--port", "9000"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MTP speculative decoding auto-enabled" in out
    assert "llama-server" in out
    assert "--port 9000" in out


def test_cli_main_no_mtp_flag_skips_note_and_disables(stub_gguf_source, fixed_server_bin, capsys):
    stub_gguf_source("m.gguf", ["blk.46.nextn.eh_proj.weight"])
    rc = serving._main(["m.gguf", "--no-mtp"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "MTP speculative decoding auto-enabled" not in out
    assert "-md" not in out


# ── hf_upload model-card MTP section ────────────────────────────────────────────

def test_model_card_has_no_mtp_section_without_nextn(tmp_path, stub_gguf_source):
    import hf_upload

    gguf = tmp_path / "model-Q4.gguf"
    gguf.write_bytes(b"gguf")
    stub_gguf_source(str(gguf), ["blk.0.attn_q.weight"])

    cfg = hf_upload.HFUploadConfig(repo_id="user/model", did_magicquant=True)
    card = hf_upload.generate_model_card(cfg, [(gguf, gguf.name)])
    assert "MTP" not in card


def test_model_card_adds_mtp_section_with_serve_command(tmp_path, stub_gguf_source, fixed_server_bin):
    import hf_upload

    gguf = tmp_path / "model-Q4.gguf"
    gguf.write_bytes(b"gguf")
    stub_gguf_source(str(gguf), ["blk.46.nextn.eh_proj.weight"])

    cfg = hf_upload.HFUploadConfig(repo_id="user/model", did_magicquant=True)
    card = hf_upload.generate_model_card(cfg, [(gguf, gguf.name)])

    assert "## Serving: MTP Speculative Decoding" in card
    assert "~1.6-1.9x" in card
    assert "2x the model's memory" in card
    assert "--spec-type draft-mtp" in card
    assert str(gguf) in card


def test_model_card_mtp_section_picks_first_mtp_gguf_among_several(tmp_path, stub_gguf_source, fixed_server_bin):
    import hf_upload

    plain = tmp_path / "model-Q4.gguf"
    plain.write_bytes(b"gguf")
    mtp = tmp_path / "model-Q6.gguf"
    mtp.write_bytes(b"gguf")
    stub_gguf_source(str(plain), ["blk.0.attn_q.weight"])
    stub_gguf_source(str(mtp), ["blk.46.nextn.eh_proj.weight"])

    cfg = hf_upload.HFUploadConfig(repo_id="user/model", did_magicquant=True)
    card = hf_upload.generate_model_card(cfg, [(plain, plain.name), (mtp, mtp.name)])

    assert "## Serving: MTP Speculative Decoding" in card
    assert str(mtp) in card
    assert str(plain) not in card.split("## Serving")[1]


# ── /api/serve-command UI endpoint ──────────────────────────────────────────────

def test_serve_command_endpoint_lists_ggufs_with_mtp_flag(tmp_path, monkeypatch, stub_gguf_source, fixed_server_bin):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module, "FOUNDRY_DIR", tmp_path)
    monkeypatch.setattr(app_module, "API_KEY", "")
    model_dir = tmp_path / "output" / "my-model" / "magicquant"
    model_dir.mkdir(parents=True)
    gguf = model_dir / "my-model-Q4.gguf"
    gguf.write_bytes(b"gguf")
    stub_gguf_source(str(gguf), ["blk.46.nextn.eh_proj.weight"])

    client = TestClient(app_module.app)
    r = client.get("/api/serve-command/my-model")
    assert r.status_code == 200
    data = r.json()
    assert data["model"] == "my-model"
    assert data["commands"] == [{
        "name": "my-model-Q4.gguf",
        "mtp": True,
        "command": serving.format_serve_command(serving.build_serve_command(str(gguf))),
    }]


def test_serve_command_endpoint_404_for_unknown_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module, "FOUNDRY_DIR", tmp_path)
    monkeypatch.setattr(app_module, "API_KEY", "")
    client = TestClient(app_module.app)
    r = client.get("/api/serve-command/nope")
    assert r.status_code == 404


def test_serve_command_endpoint_rejects_invalid_model_name(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import app as app_module

    monkeypatch.setattr(app_module, "FOUNDRY_DIR", tmp_path)
    monkeypatch.setattr(app_module, "API_KEY", "")
    client = TestClient(app_module.app)
    r = client.get("/api/serve-command/bad;name")
    assert r.status_code == 400
