"""L-hf-token-scope: HF token only enters the env for upload (or opt-in).

We exercise the env-building logic in run_script by capturing the subprocess env
through a fake create_subprocess_exec. No real model/upload.
"""

import asyncio
import os
from pathlib import Path

import pytest

import app as app_module


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = self  # async-iterable yielding nothing

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def wait(self):
        return 0


@pytest.fixture
def capture_env(monkeypatch, tmp_path):
    captured = {}

    async def fake_exec(*args, env=None, **kwargs):
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(app_module.asyncio, "create_subprocess_exec", fake_exec)
    # Pretend a cached HF token file exists so injection has something to inject.
    token_file = tmp_path / "token"
    token_file.write_text("hf_cached_token")
    real_home = Path.home

    def fake_home():
        return tmp_path.parent  # not used directly; we patch the path below

    # Force the token path used by run_script to our temp file.
    monkeypatch.setattr(app_module.Path, "home", staticmethod(lambda: tmp_path.parent))
    (tmp_path.parent / ".cache" / "huggingface").mkdir(parents=True, exist_ok=True)
    (tmp_path.parent / ".cache" / "huggingface" / "token").write_text("hf_cached_token")
    return captured


def _run_script(out_dir, **kw):
    return asyncio.run(app_module.run_script("print('x')\n", str(out_dir), **kw))


def test_training_script_has_no_hf_token(capture_env, monkeypatch, tmp_path):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("FOUNDRY_HF_TOKEN_ALL_STAGES", raising=False)
    _run_script(tmp_path, inject_hf_token=False)
    assert "HF_TOKEN" not in capture_env["env"]


def test_upload_script_gets_hf_token(capture_env, monkeypatch, tmp_path):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    _run_script(tmp_path, inject_hf_token=True)
    assert capture_env["env"].get("HF_TOKEN") == "hf_cached_token"


def test_all_stages_optin(capture_env, monkeypatch, tmp_path):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("FOUNDRY_HF_TOKEN_ALL_STAGES", "1")
    _run_script(tmp_path, inject_hf_token=False)
    assert capture_env["env"].get("HF_TOKEN") == "hf_cached_token"


def test_inherited_token_stripped_for_non_upload(capture_env, monkeypatch, tmp_path):
    """An inherited HF_TOKEN must not leak into a training subprocess."""
    monkeypatch.setenv("HF_TOKEN", "hf_inherited")
    monkeypatch.delenv("FOUNDRY_HF_TOKEN_ALL_STAGES", raising=False)
    _run_script(tmp_path, inject_hf_token=False)
    assert "HF_TOKEN" not in capture_env["env"]
