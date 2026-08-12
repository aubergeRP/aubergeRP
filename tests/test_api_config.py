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


# ---------------------------------------------------------------------------
# Extended sections: chat / scheduler / observability / gui
# ---------------------------------------------------------------------------


def test_get_config_exposes_all_sections(client):
    """Every settings group must be reachable from the admin API."""
    data = client.get("/api/config/").json()
    assert set(data) == {
        "app",
        "user",
        "active_connectors",
        "chat",
        "scheduler",
        "observability",
        "gui",
    }
    assert data["chat"]["context_window"] == 4096
    assert data["chat"]["summarization_threshold"] == 0.75
    assert data["scheduler"]["cleanup_older_than_days"] == 30
    assert data["observability"]["metrics_enabled"] is False
    assert data["gui"]["public_character_list"] is True


def test_get_config_never_exposes_admin_secrets(client):
    app_data = client.get("/api/config/").json()["app"]
    assert "admin_password_hash" not in app_data
    assert "admin_jwt_secret" not in app_data
    assert "data_dir" in app_data  # informational, read-only


def test_put_round_trips_new_sections(client):
    resp = client.put(
        "/api/config/",
        json={
            "chat": {
                "context_window": 16384,
                "summarization_threshold": 0.5,
                "ooc_protection": False,
                "image_autonomy": False,
                "image_autonomy_cooldown": 0,
            },
            "scheduler": {
                "enabled": True,
                "interval_seconds": 3600,
                "cleanup_older_than_days": 7,
                "health_check_enabled": False,
                "health_check_interval_seconds": 60,
            },
            "observability": {"metrics_enabled": True},
            "gui": {"public_character_list": False},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["chat"]["context_window"] == 16384
    assert data["chat"]["ooc_protection"] is False
    assert data["scheduler"]["enabled"] is True
    assert data["scheduler"]["cleanup_older_than_days"] == 7
    assert data["observability"]["metrics_enabled"] is True
    assert data["gui"]["public_character_list"] is False
    # Survives a reload from disk.
    assert client.get("/api/config/").json() == data


def test_put_app_accepts_sentry_and_token_ttl(client):
    resp = client.put(
        "/api/config/",
        json={
            "app": {
                "host": "127.0.0.1",
                "port": 9000,
                "log_level": "DEBUG",
                "sentry_dsn": "https://key@sentry.example/1",
                "admin_token_ttl_seconds": 3600,
            }
        },
    )
    assert resp.status_code == 200
    app_data = resp.json()["app"]
    assert app_data["sentry_dsn"] == "https://key@sentry.example/1"
    assert app_data["admin_token_ttl_seconds"] == 3600


def test_put_ignores_read_only_data_dir(client):
    before = client.get("/api/config/").json()["app"]["data_dir"]
    resp = client.put(
        "/api/config/",
        json={
            "app": {
                "host": "0.0.0.0",
                "port": 8123,
                "log_level": "INFO",
                "data_dir": "/somewhere/else",
            }
        },
    )
    assert resp.status_code == 200
    assert resp.json()["app"]["data_dir"] == before


@pytest.mark.parametrize(
    "payload",
    [
        {"chat": {"summarization_threshold": 1.5}},
        {"chat": {"summarization_threshold": 0}},
        {"chat": {"context_window": 0}},
        {"chat": {"image_autonomy_cooldown": -1}},
        {"scheduler": {"interval_seconds": 0}},
        {"scheduler": {"cleanup_older_than_days": 0}},
        {"scheduler": {"health_check_interval_seconds": 0}},
        {"app": {"admin_token_ttl_seconds": 0}},
        {"app": {"port": 0}},
        {"app": {"port": 70000}},
    ],
)
def test_patch_rejects_out_of_range_values(client, payload):
    assert client.patch("/api/config/", json=payload).status_code == 422


def test_patch_only_touches_provided_fields(client):
    client.put(
        "/api/config/",
        json={
            "chat": {
                "context_window": 8192,
                "summarization_threshold": 0.6,
                "ooc_protection": True,
                "image_autonomy": True,
                "image_autonomy_cooldown": 2,
            }
        },
    )
    resp = client.patch("/api/config/", json={"chat": {"ooc_protection": False}})
    assert resp.status_code == 200
    chat = resp.json()["chat"]
    assert chat["ooc_protection"] is False
    assert chat["context_window"] == 8192
    assert chat["summarization_threshold"] == 0.6
    assert chat["image_autonomy_cooldown"] == 2


def test_patch_public_character_list(client):
    resp = client.patch("/api/config/", json={"gui": {"public_character_list": False}})
    assert resp.status_code == 200
    assert resp.json()["gui"]["public_character_list"] is False
    # And it reaches the dedicated GUI endpoint too — one setting, one source.
    assert client.get("/api/config/gui").json()["public_character_list"] is False
