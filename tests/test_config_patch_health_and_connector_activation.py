"""
Tests for config patch merge semantics, connector health state,
persisted connector test results, and auto-activation rules.

Covers:
  1. PATCH /api/config — per-field partial merge semantics
    2. Health endpoint: "connected" field is null before first connector test
  3. _last_test_results persisted to disk and survives restart
  4. POST /api/connectors/ auto-activates first connector of its type
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aubergeRP.config import Config, reset_config
from aubergeRP.connectors.manager import ConnectorManager  # noqa: F401
from aubergeRP.main import create_app
from aubergeRP.models.connector import ConnectorCreate
from aubergeRP.routers import config as config_router
from aubergeRP.routers.config import get_config_save_path
from aubergeRP.routers.connectors import _TestResultsStore, get_connector_manager

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def make_manager(tmp_path: Path, active_text: str = "", active_image: str = "") -> ConnectorManager:
    config = Config()
    config.app.data_dir = str(tmp_path)
    config.active_connectors.text = active_text
    config.active_connectors.image = active_image
    return ConnectorManager(data_dir=tmp_path, config=config, config_path=tmp_path / "config.yaml")


TEXT_PAYLOAD = {
    "name": "My Ollama",
    "type": "text",
    "backend": "openai_api",
    "config": {"base_url": "http://localhost:11434/v1", "model": "llama3", "api_key": "secret"},
}
IMAGE_PAYLOAD = {
    "name": "My Image Gen",
    "type": "image",
    "backend": "openai_api",
    "config": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "gemini-flash",
        "api_key": "sk-or-x",
    },
}


# ===========================================================================
# 1. PATCH /api/config — per-field partial merge
# ===========================================================================


@pytest.fixture
def config_client(tmp_path):
    # Ensure tests do not depend on repo-local config.yaml values.
    reset_config()
    from aubergeRP import config as config_module
    config_file = tmp_path / "config.yaml"
    # get_config() reloads from _config_path whenever its mtime differs from the
    # last-seen one, so the seeded singleton must be paired with a tmp path —
    # otherwise the repo-local config.yaml gets loaded on the first request.
    original_path = config_module._config_path
    config_module._config_path = config_file
    seeded = Config()
    seeded.app.data_dir = str(tmp_path)
    config_module._config = seeded
    config_module._config_mtime = 0.0

    app = create_app()
    app.dependency_overrides[get_config_save_path] = lambda: config_file
    with TestClient(app) as c:
        yield c
    config_module._config_path = original_path
    reset_config()


def test_patch_user_name_only(config_client):
    """PATCH with only user.name must not touch app or active_connectors."""
    resp = config_client.patch("/api/config/", json={"user": {"name": "Frodo"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["name"] == "Frodo"
    # Defaults must be unchanged
    assert data["app"]["port"] == 8123
    assert data["app"]["host"] == "0.0.0.0"
    assert data["active_connectors"]["text"] == ""


def test_patch_app_port_only(config_client):
    """PATCH only app.port — other app fields must stay at defaults."""
    resp = config_client.patch("/api/config/", json={"app": {"port": 9999}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["app"]["port"] == 9999
    assert data["app"]["host"] == "0.0.0.0"   # default preserved
    assert data["app"]["log_level"] == "INFO"  # default preserved


def test_patch_active_connectors_text_only(config_client):
    """PATCH active_connectors.text — image must remain unchanged."""
    # First set image via PUT so we have a non-empty value to preserve
    config_client.put(
        "/api/config/",
        json={
            "app": {"host": "0.0.0.0", "port": 8123, "log_level": "INFO"},
            "user": {"name": "User"},
            "active_connectors": {"text": "", "image": "img-uuid"},
        },
    )
    resp = config_client.patch("/api/config/", json={"active_connectors": {"text": "txt-uuid"}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_connectors"]["text"] == "txt-uuid"
    assert data["active_connectors"]["image"] == "img-uuid"  # preserved


def test_patch_per_task_text_connectors(config_client, tmp_path, monkeypatch):
    """Multi-model: per-task overrides accept a text connector and persist."""
    manager = make_manager(tmp_path)
    monkeypatch.setattr(config_router, "get_connector_manager", lambda: manager)
    text_id = manager.create_connector(ConnectorCreate(**TEXT_PAYLOAD)).id

    resp = config_client.patch(
        "/api/config/",
        json={"active_connectors": {"text_summarization": text_id}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["active_connectors"]["text_summarization"] == text_id
    # Unset roles stay empty → "same as main model".
    assert data["active_connectors"]["text_utility"] == ""


def test_patch_per_task_connector_rejects_unknown_id(config_client, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(config_router, "get_connector_manager", lambda: manager)

    resp = config_client.patch(
        "/api/config/",
        json={"active_connectors": {"text_utility": "nope"}},
    )
    assert resp.status_code == 400


def test_patch_per_task_connector_rejects_image_connector(config_client, tmp_path, monkeypatch):
    manager = make_manager(tmp_path)
    monkeypatch.setattr(config_router, "get_connector_manager", lambda: manager)
    image_id = manager.create_connector(ConnectorCreate(**IMAGE_PAYLOAD)).id

    resp = config_client.patch(
        "/api/config/",
        json={"active_connectors": {"text_utility": image_id}},
    )
    assert resp.status_code == 400


def test_patch_empty_body_is_no_op(config_client):
    """An empty PATCH body must return the current config unchanged."""
    before = config_client.get("/api/config/").json()
    resp = config_client.patch("/api/config/", json={})
    assert resp.status_code == 200
    assert resp.json() == before


def test_patch_persists_to_disk(config_client, tmp_path):
    """PATCH must write changes to the config file on disk."""
    import yaml

    config_file = tmp_path / "config.yaml"
    config_client.patch("/api/config/", json={"user": {"name": "Bilbo"}})
    assert config_file.exists()
    saved = yaml.safe_load(config_file.read_text())
    assert saved["user"]["name"] == "Bilbo"


def test_patch_invalid_log_level(config_client):
    """PATCH with an invalid log_level must be rejected with 422."""
    resp = config_client.patch("/api/config/", json={"app": {"log_level": "VERBOSE"}})
    assert resp.status_code == 422


def test_patch_multiple_fields(config_client):
    """PATCH can update multiple sub-objects in one call."""
    resp = config_client.patch(
        "/api/config/",
        json={"user": {"name": "Aragorn"}, "app": {"port": 7777}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["name"] == "Aragorn"
    assert data["app"]["port"] == 7777
    assert data["app"]["host"] == "0.0.0.0"  # untouched default


# ===========================================================================
# 2. Health endpoint: "connected" is null before first test
# ===========================================================================


@pytest.fixture
def health_client(tmp_path):
    from aubergeRP.config import reset_config
    reset_config()
    app = create_app()
    manager = make_manager(tmp_path)
    app.dependency_overrides[get_connector_manager] = lambda: manager
    with TestClient(app) as c:
        yield c, manager


def test_health_connected_null_before_any_test(health_client, tmp_path):
    """connected field must be null (None) before any /test call is made."""
    client, manager = health_client

    # Create a text connector and activate it
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    connector_id = created["id"]
    client.post(f"/api/connectors/{connector_id}/activate")

    # Patch the config singleton so health endpoint sees this connector as active
    from aubergeRP.config import get_config
    get_config().active_connectors.text = connector_id
    get_config().app.data_dir = str(tmp_path)

    resp = client.get("/api/health/")
    assert resp.status_code == 200
    data = resp.json()
    text_info = data["connectors"]["text"]
    assert text_info is not None
    assert text_info["connected"] is None, "connected must be null before any test"


def test_health_connected_not_null_after_test(tmp_path):
    """After a test result is recorded, connected must be a bool (True or False)."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    from aubergeRP.routers.connectors import _last_test_results

    connector_id = "fake-uuid"
    _last_test_results.set(connector_id, True)

    # The store must return True (a boolean), not None
    result = _last_test_results.get(connector_id)
    assert result is True
    assert isinstance(result, bool)

    # Clean up
    _last_test_results.pop(connector_id, None)


# ===========================================================================
# 3. _last_test_results persisted to disk and survives restart
# ===========================================================================


def test_test_results_persist_to_disk(tmp_path, monkeypatch):
    """set() must write connector_test_results.json to data_dir."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    store = _TestResultsStore()
    store.set("abc", True)

    results_file = tmp_path / "connector_test_results.json"
    assert results_file.exists()
    data = json.loads(results_file.read_text())
    assert data["abc"]["connected"] is True
    assert data["abc"]["checked_at"]


def test_test_results_loaded_from_disk(tmp_path):
    """A new store instance reads existing results from disk (restart simulation)."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    # Write results as first "process"
    store1 = _TestResultsStore()
    store1.set("connector-1", True)
    store1.set("connector-2", False)

    # Simulate restart — create a fresh store instance
    store2 = _TestResultsStore()
    assert store2.get("connector-1") is True
    assert store2.get("connector-2") is False


def test_test_results_default_none_for_unknown(tmp_path):
    """get() returns None for unknown connector IDs (not False)."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    store = _TestResultsStore()
    assert store.get("nonexistent") is None


def test_test_results_pop_removes_entry(tmp_path):
    """pop() must remove the entry from disk."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    store = _TestResultsStore()
    store.set("del-me", True)
    store.pop("del-me", None)

    store2 = _TestResultsStore()
    assert store2.get("del-me") is None


def test_delete_connector_removes_test_result(tmp_path):
    """Deleting a connector via the API must also remove its persisted test result."""
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)

    app = create_app()
    manager = make_manager(tmp_path)
    app.dependency_overrides[get_connector_manager] = lambda: manager
    with TestClient(app) as client:
        created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
        cid = created["id"]
        # Manually plant a test result
        _TestResultsStore().set(cid, True)
        # Delete connector
        client.delete(f"/api/connectors/{cid}")

    # After deletion the entry should be gone
    from aubergeRP.config import get_config
    get_config().app.data_dir = str(tmp_path)
    store = _TestResultsStore()
    assert store.get(cid) is None


# ===========================================================================
# 4. Auto-activate first connector of its type on creation
# ===========================================================================


@pytest.fixture
def connector_client(tmp_path):
    from aubergeRP.config import reset_config
    reset_config()
    app = create_app()
    manager = make_manager(tmp_path)
    app.dependency_overrides[get_connector_manager] = lambda: manager
    with TestClient(app) as c:
        yield c, manager


def test_first_text_connector_auto_activated(connector_client):
    """Creating the first text connector must auto-activate it."""
    client, manager = connector_client
    resp = client.post("/api/connectors/", json=TEXT_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_active"] is True
    assert manager._config.active_connectors.text == data["id"]


def test_first_image_connector_auto_activated(connector_client):
    """Creating the first image connector must auto-activate it."""
    client, manager = connector_client
    resp = client.post("/api/connectors/", json=IMAGE_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["is_active"] is True
    assert manager._config.active_connectors.image == data["id"]


def test_second_text_connector_not_auto_activated(connector_client):
    """Second text connector must NOT replace the active connector."""
    client, manager = connector_client
    first = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    second_payload = {**TEXT_PAYLOAD, "name": "Second Ollama"}
    second = client.post("/api/connectors/", json=second_payload).json()
    assert second["is_active"] is False
    # First remains active
    assert manager._config.active_connectors.text == first["id"]


def test_text_and_image_auto_activate_independently(connector_client):
    """Text and image types are auto-activated independently."""
    client, manager = connector_client
    text = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    image = client.post("/api/connectors/", json=IMAGE_PAYLOAD).json()
    assert text["is_active"] is True
    assert image["is_active"] is True
    assert manager._config.active_connectors.text == text["id"]
    assert manager._config.active_connectors.image == image["id"]


def test_auto_activate_reflected_in_get(connector_client):
    """Auto-activated connector must appear as active in GET response."""
    client, _ = connector_client
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    detail = client.get(f"/api/connectors/{created['id']}").json()
    assert detail["is_active"] is True


def test_get_active_id_for_type(tmp_path):
    """ConnectorManager.get_active_id_for_type returns correct IDs."""
    manager = make_manager(tmp_path, active_text="txt-id", active_image="img-id")
    assert manager.get_active_id_for_type("text") == "txt-id"
    assert manager.get_active_id_for_type("image") == "img-id"
    assert manager.get_active_id_for_type("video") == ""
    assert manager.get_active_id_for_type("audio") == ""
