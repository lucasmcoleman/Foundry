"""HF token resolution.

Regression: an overnight Qwen run finished a multi-hour measured search and
then failed at the upload step with "HF_TOKEN environment variable is not
set" -- on a machine that was authenticated the whole time. Only the env var
was consulted, while `huggingface-cli login` stores the token in
~/.cache/huggingface/token, which every download in the same pipeline had
been using. Discovering that after the expensive part is the worst case.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from hf_upload import _resolve_hf_token


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "env-token")
    assert _resolve_hf_token() == "env-token"


def test_falls_back_to_credential_store(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: "stored-token")
    assert _resolve_hf_token() == "stored-token"


def test_empty_env_var_does_not_shadow_the_store(monkeypatch):
    # An exported-but-empty HF_TOKEN is a common shell accident and must not
    # mask a perfectly good logged-in credential.
    monkeypatch.setenv("HF_TOKEN", "")
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: "stored-token")
    assert _resolve_hf_token() == "stored-token"


def test_none_when_neither_available(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    assert _resolve_hf_token() is None
