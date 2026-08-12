"""Integration tests for /api/config via FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aubergeRP.main import create_app
from aubergeRP.routers.config import get_config_save_path


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Ensure tests don't read the developer's local config.yaml from repo root.
    monkeypatch.chdir(tmp_path)
    app = create_app()
    config_file = tmp_path / "config.yaml"
    app.dependency_overrides[get_config_save_path] = lambda: config_file
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/config/
# ---------------------------------------------------------------------------


def test_get_config_default(client):
    resp = client.get("/api/config/")
    assert resp.status_code == 200
    data = resp.json()
    assert "app" in data
    assert "user" in data
    assert "active_connectors" in data
    assert data["app"]["host"] == "0.0.0.0"
    assert data["app"]["port"] == 8123
    assert data["user"]["name"] == "User"
    assert data["active_connectors"]["text"] == ""
    assert data["active_connectors"]["image"] == ""


def test_get_config_shape(client):
    resp = client.get("/api/config/")
    data = resp.json()
    assert set(data["app"].keys()) >= {"host", "port", "log_level"}
    assert "name" in data["user"]
    assert "text" in data["active_connectors"]
    assert "image" in data["active_connectors"]


# ---------------------------------------------------------------------------
# PUT /api/config/
# ---------------------------------------------------------------------------


def test_update_user_name(client):
    resp = client.put(
        "/api/config/",
        json={
            "app": {"host": "0.0.0.0", "port": 8123, "log_level": "INFO"},
            "user": {"name": "Gandalf"},
            "active_connectors": {"text": "", "image": ""},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["name"] == "Gandalf"


def test_update_app_port(client):
    resp = client.put(
        "/api/config/",
        json={
            "app": {"host": "127.0.0.1", "port": 9000, "log_level": "DEBUG"},
            "user": {"name": "User"},
            "active_connectors": {"text": "", "image": ""},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["app"]["port"] == 9000
    assert data["app"]["host"] == "127.0.0.1"
    assert data["app"]["log_level"] == "DEBUG"


def test_update_active_connectors(client):
    resp = client.put(
        "/api/config/",
        json={
            "app": {"host": "0.0.0.0", "port": 8123, "log_level": "INFO"},
            "user": {"name": "User"},
            "active_connectors": {"text": "some-uuid", "image": "other-uuid"},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_connectors"]["text"] == "some-uuid"
    assert data["active_connectors"]["image"] == "other-uuid"


def test_update_partial_null_fields(client):
    """Null fields should be no-op (no overwrite)."""
    resp = client.put(
        "/api/config/",
        json={
            "app": None,
            "user": {"name": "Frodo"},
            "active_connectors": None,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["name"] == "Frodo"
    # unchanged
    assert data["app"]["port"] == 8123


def test_update_config_persisted(client, tmp_path):
    import yaml

    config_file = tmp_path / "config.yaml"
    client.put(
        "/api/config/",
        json={
            "app": {"host": "0.0.0.0", "port": 8123, "log_level": "INFO"},
            "user": {"name": "Bilbo"},
            "active_connectors": {"text": "", "image": ""},
        },
    )
    assert config_file.exists()
    with config_file.open() as f:
        saved = yaml.safe_load(f)
    assert saved["user"]["name"] == "Bilbo"


def test_update_config_readonly_file(client, tmp_path):
    """A non-writable config.yaml yields an explicit message, not a bare 500."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("")
    config_file.chmod(0o400)
    try:
        resp = client.patch("/api/config/", json={"user": {"name": "Frodo"}})
    finally:
        config_file.chmod(0o600)
    assert resp.status_code == 500
    assert resp.json()["detail"] == "config.yaml is not writable"
