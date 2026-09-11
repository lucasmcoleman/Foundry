"""Browser-origin boundaries and installed-package configuration persistence."""
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as ui


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ui, "API_KEY", "")
    monkeypatch.setattr(ui, "REQUIRE_AUTH", False)
    return TestClient(ui.app)


def test_foreign_origin_cannot_read_state_or_stream_logs(client):
    assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws", headers={"Origin": "https://evil.example"}):
            pass
    assert error.value.code == 4003


def test_allowed_origin_and_nonbrowser_clients_work(client):
    assert client.get("/api/state", headers={"Origin": ui.ALLOWED_ORIGINS[0]}).status_code == 200
    with client.websocket_connect("/ws", headers={"Origin": ui.ALLOWED_ORIGINS[0]}):
        pass
    assert client.get("/api/state").status_code == 200


def test_websocket_invalid_unicode_token_is_rejected_cleanly(client, monkeypatch):
    monkeypatch.setattr(ui, "API_KEY", "secret")
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws?token=%C3%A9"):
            pass
    assert error.value.code == 4001


def test_health_discloses_required_auth_without_configured_key(client, monkeypatch):
    monkeypatch.setattr(ui, "REQUIRE_AUTH", True)
    assert client.get("/health").json()["auth_enabled"] is True
    assert client.get("/api/state").status_code == 401


def test_config_creates_writable_parent_and_handles_nonobject_json(client, monkeypatch, tmp_path):
    path = tmp_path / "settings" / "config.json"
    monkeypatch.setattr(ui, "CONFIG_PATH", path)
    result = client.post("/api/config", json={"hf_username": "alice"})
    assert result.status_code == 200
    assert json.loads(path.read_text()) == {"hf_username": "alice"}
    path.write_text("[]")
    assert client.get("/api/config").json() == {}
    assert client.post("/api/config", json={"hf_username": "bob"}).status_code == 200


def test_ui_dataset_upload_requires_explicit_optin():
    assert ui.UploadCfg().upload_dataset is False
    assert ui.UploadCfg(upload_dataset=True).upload_dataset is True
