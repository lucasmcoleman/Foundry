"""H3 / M-rce-auth / L-timing-compare / L-config-post: UI auth, bind, config.

Uses FastAPI's TestClient (httpx) — no network/GPU. The auth dependency reads
the module-global API_KEY, so we patch it per test.
"""

import importlib

import pytest
from fastapi.testclient import TestClient

import app as app_module  # ui/app.py (ui is on sys.path via conftest)


@pytest.fixture
def client():
    return TestClient(app_module.app)


# ── Bind / host selector (H3) ────────────────────────────────────────────────

def test_host_defaults_to_loopback():
    assert app_module.select_host(None, "") == "127.0.0.1"
    assert app_module.select_host("127.0.0.1", "") == "127.0.0.1"
    assert app_module.select_host("localhost", "") == "localhost"


def test_non_loopback_without_key_is_refused():
    with pytest.raises(SystemExit):
        app_module.select_host("0.0.0.0", "")


def test_non_loopback_with_key_is_allowed():
    assert app_module.select_host("0.0.0.0", "secret") == "0.0.0.0"


def test_non_loopback_allowed_when_require_auth():
    assert app_module.select_host("0.0.0.0", "", require_auth=True) == "0.0.0.0"


# ── Unauthenticated endpoints ────────────────────────────────────────────────

def test_health_is_unauthenticated_and_reports_auth(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "secret")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is True


def test_health_reports_auth_disabled_when_no_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["auth_enabled"] is False


# ── Protected endpoints (H3 + M-rce-auth) ────────────────────────────────────

def test_state_requires_bearer_when_key_set(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "secret")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", False)
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/api/state", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_run_endpoint_requires_bearer_when_key_set(client, monkeypatch):
    """M-rce-auth: /api/run (shell-equivalent) must 401 without the key."""
    monkeypatch.setattr(app_module, "API_KEY", "secret")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", False)
    r = client.post("/api/run", json={})
    assert r.status_code == 401


def test_no_key_allows_state_by_default(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", False)
    assert client.get("/api/state").status_code == 200


def test_require_auth_fails_closed_without_key(client, monkeypatch):
    monkeypatch.setattr(app_module, "API_KEY", "")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", True)
    assert client.get("/api/state").status_code == 401


# ── POST /api/config validation (L-config-post) ──────────────────────────────

def test_config_rejects_unexpected_keys(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "API_KEY", "")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", False)
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    r = client.post("/api/config", json={"evil_key": "x"})
    assert r.status_code == 422


def test_config_accepts_known_key(client, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "API_KEY", "")
    monkeypatch.setattr(app_module, "REQUIRE_AUTH", False)
    monkeypatch.setattr(app_module, "CONFIG_PATH", tmp_path / "config.json")
    r = client.post("/api/config", json={"hf_username": "alice"})
    assert r.status_code == 200
    assert r.json().get("hf_username") == "alice"
