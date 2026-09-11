"""Publication failures must not become successful stage completion markers."""

from types import SimpleNamespace

import httpx
import huggingface_hub
import pytest
import requests
from tenacity import wait_none

import hf_upload


def test_dataset_publication_requires_explicit_opt_in():
    import pipeline

    assert hf_upload.HFUploadConfig().upload_dataset is False
    config = pipeline.PipelineConfig(
        upload=pipeline.UploadConfig(repo_id="review/model", license="mit")
    )
    built = pipeline._build_hf_upload_config(config, lambda *a: None)
    assert built.upload_dataset is False
    config.upload.upload_dataset = True
    assert pipeline._build_hf_upload_config(config, lambda *a: None).upload_dataset is True
    assert pipeline.build_arg_parser().parse_args(["--upload-dataset"]).upload_dataset is True


def _http_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://hub.invalid"))
    return httpx.HTTPStatusError("test failure", request=response.request, response=response)


@pytest.mark.parametrize("error", [
    httpx.ConnectError("offline"), httpx.ReadTimeout("timed out"),
    requests.ConnectionError("offline"), _http_error(429), _http_error(503),
])
def test_hub_retry_handles_both_http_clients(error):
    calls = []

    def whoami():
        calls.append(True)
        if len(calls) == 1:
            raise error
        return {"name": "review"}

    check = hf_upload._whoami_with_retry.retry_with(wait=wait_none())
    assert check(SimpleNamespace(whoami=whoami)) == {"name": "review"}
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_hub_retry_does_not_repeat_permanent_errors(status):
    calls = []
    error = _http_error(status)

    def whoami():
        calls.append(True)
        raise error

    check = hf_upload._whoami_with_retry.retry_with(wait=wait_none())
    with pytest.raises(httpx.HTTPStatusError) as caught:
        check(SimpleNamespace(whoami=whoami))
    assert caught.value is error
    assert len(calls) == 1


@pytest.fixture
def upload_case(monkeypatch, tmp_path):
    out = tmp_path / "magicquant"
    out.mkdir()
    (out / "Model-Q4.gguf").write_bytes(b"GGUF")
    state = SimpleNamespace(files=[], events=[], fail="", listing_calls=0)

    class API:
        def __init__(self, **kwargs):
            pass

        def whoami(self):
            return {"name": "review"}

        def create_repo(self, **kwargs):
            state.events.append("repo")

        def repo_info(self, *args, **kwargs):
            if state.fail == "repo_info":
                raise _http_error(503)
            return SimpleNamespace(siblings=[])

        def upload_file(self, **kwargs):
            state.events.append("file")
            if state.fail == "file":
                raise ValueError("rejected file")
            state.files.append(kwargs["path_in_repo"])

    class Card:
        def __init__(self, content):
            pass

        def push_to_hub(self, *args, **kwargs):
            state.events.append("card")
            if state.fail == "card":
                raise ValueError("rejected card")

    def list_files(*args, **kwargs):
        state.listing_calls += 1
        if state.fail == "listing":
            raise httpx.ConnectError("offline")
        if state.fail == "missing" and state.listing_calls > 1:
            return []
        return list(state.files)

    monkeypatch.setattr(huggingface_hub, "HfApi", API)
    monkeypatch.setattr(huggingface_hub, "ModelCard", Card)
    monkeypatch.setattr(huggingface_hub, "list_repo_files", list_files)
    monkeypatch.setattr(hf_upload, "generate_model_card", lambda *a, **kw:
                        "| [Model-Q4.gguf](./Model-Q4.gguf) |\n")
    cfg = hf_upload.HFUploadConfig(repo_id="review/model", upload_dataset=False)
    logs = []

    def run():
        return hf_upload.upload(cfg, str(tmp_path), token="test-only",
                                log=lambda msg, level="info": logs.append((level, msg)))

    return state, cfg, run, logs, tmp_path


def test_card_publishes_only_after_verified_artifacts(upload_case):
    state, _, run, _, _ = upload_case
    assert run() is True
    assert state.events == ["repo", "file", "card"]


@pytest.mark.parametrize("failure", ["card", "file", "listing", "missing"])
def test_publication_failure_returns_false(upload_case, failure):
    state, _, run, logs, _ = upload_case
    state.fail = failure
    assert run() is False
    assert any(level == "error" for level, _ in logs)
    if failure != "card":
        assert "card" not in state.events


def test_contradictory_card_stops_before_artifact_upload(upload_case, monkeypatch):
    state, _, run, _, _ = upload_case
    monkeypatch.setattr(hf_upload, "generate_model_card", lambda *a, **kw: "No files.")
    assert run() is False
    assert state.events == ["repo"]


def test_requested_dataset_failure_returns_false(upload_case):
    state, cfg, run, _, tmp_path = upload_case
    dataset = tmp_path / "train.jsonl"
    dataset.write_text('{"text":"example"}\n')
    cfg.upload_dataset = True
    cfg.dataset_name = str(dataset)
    state.fail = "file"
    assert run() is False
    assert "card" not in state.events


def test_dry_run_does_not_treat_server_error_as_absent_repo(upload_case):
    state, cfg, _, _, tmp_path = upload_case
    state.fail = "repo_info"
    report = hf_upload.dry_run(cfg, str(tmp_path), token="test-only",
                               log=lambda *args: None)
    assert not report.ok
    assert not report.repo_accessible
    assert "verify repository access" in report.errors[0]
