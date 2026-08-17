"""Integration tests for /api/connectors via FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aubergeRP.config import Config
from aubergeRP.connectors.manager import ConnectorManager
from aubergeRP.main import create_app
from aubergeRP.routers.connectors import get_connector_manager


def make_manager(tmp_path, active_text="", active_image=""):
    config = Config()
    config.active_connectors.text = active_text
    config.active_connectors.image = active_image
    config_path = tmp_path / "config.yaml"
    return ConnectorManager(
        data_dir=tmp_path,
        config=config,
        config_path=config_path,
    )


@pytest.fixture
def client(tmp_path):
    app = create_app()
    manager = make_manager(tmp_path)
    app.dependency_overrides[get_connector_manager] = lambda: manager
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_manager(tmp_path):
    """Yields (client, manager) so tests can inspect manager state."""
    app = create_app()
    manager = make_manager(tmp_path)
    app.dependency_overrides[get_connector_manager] = lambda: manager
    with TestClient(app) as c:
        yield c, manager


TEXT_PAYLOAD = {
    "name": "My Ollama",
    "type": "text",
    "backend": "openai_api",
    "config": {
        "base_url": "http://localhost:11434/v1",
        "model": "llama3",
        "api_key": "secret",
    },
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


# ---------------------------------------------------------------------------
# GET /api/connectors/
# ---------------------------------------------------------------------------


def test_list_empty(client):
    resp = client.get("/api/connectors/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_with_type_filter(client):
    client.post("/api/connectors/", json=TEXT_PAYLOAD)
    client.post("/api/connectors/", json=IMAGE_PAYLOAD)
    resp = client.get("/api/connectors/?type=text")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["type"] == "text"


# ---------------------------------------------------------------------------
# POST /api/connectors/
# ---------------------------------------------------------------------------


def test_create_connector_text(client):
    resp = client.post("/api/connectors/", json=TEXT_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Ollama"
    assert data["type"] == "text"
    assert data["backend"] == "openai_api"
    assert "id" in data
    assert "created_at" in data


def test_create_connector_api_key_redacted(client):
    resp = client.post("/api/connectors/", json=TEXT_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert "api_key" not in data["config"]
    assert data["config"]["api_key_set"] is True


def test_create_connector_no_api_key(client):
    payload = dict(TEXT_PAYLOAD)
    payload["config"] = {**payload["config"], "api_key": ""}
    resp = client.post("/api/connectors/", json=payload)
    assert resp.status_code == 201
    assert resp.json()["config"]["api_key_set"] is False


def test_create_connector_nsfw_defaults_to_false(client):
    resp = client.post("/api/connectors/", json=TEXT_PAYLOAD)
    assert resp.status_code == 201
    assert resp.json()["config"]["nsfw"] is False


def test_create_connector_persists_max_retries(client):
    payload = dict(TEXT_PAYLOAD)
    payload["config"] = {**payload["config"], "max_retries": 5}
    resp = client.post("/api/connectors/", json=payload)
    assert resp.status_code == 201
    connector_id = resp.json()["id"]
    stored = client.get(f"/api/connectors/{connector_id}").json()
    assert stored["config"]["max_retries"] == 5


def test_backends_expose_max_retries_schema(client):
    backends = client.get("/api/connectors/backends").json()
    for backend in backends:
        assert "max_retries" in backend["config_schema"]["common"]


def test_backends_expose_custom_json_schema(client):
    backends = client.get("/api/connectors/backends").json()
    for backend in backends:
        field = backend["config_schema"]["common"]["custom_json"]
        assert field["type"] == "object"


def test_create_connector_unknown_backend(client):
    bad = {**TEXT_PAYLOAD, "backend": "unknown_backend"}
    resp = client.post("/api/connectors/", json=bad)
    # Pydantic rejects unknown Literal backend with 422
    assert resp.status_code in (400, 422)


def test_create_connector_first_is_active_auto(client):
    """First connector of a type is automatically activated."""
    resp = client.post("/api/connectors/", json=TEXT_PAYLOAD)
    assert resp.json()["is_active"] is True


def test_create_connector_second_not_active(client):
    """Second connector of the same type is NOT auto-activated."""
    client.post("/api/connectors/", json=TEXT_PAYLOAD)
    second = {**TEXT_PAYLOAD, "name": "Second Ollama"}
    resp = client.post("/api/connectors/", json=second)
    assert resp.json()["is_active"] is False


# ---------------------------------------------------------------------------
# GET /api/connectors/{id}
# ---------------------------------------------------------------------------


def test_get_connector(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    resp = client.get(f"/api/connectors/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_connector_not_found(client):
    resp = client.get("/api/connectors/nonexistent-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/connectors/{id}
# ---------------------------------------------------------------------------


def test_update_connector(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    update = {**TEXT_PAYLOAD, "name": "Updated Name"}
    resp = client.put(f"/api/connectors/{created['id']}", json=update)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Name"


def test_update_preserves_api_key_if_omitted(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    update = {
        "name": "My Ollama",
        "type": "text",
        "backend": "openai_api",
        "config": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3",
            # api_key deliberately omitted
        },
    }
    resp = client.put(f"/api/connectors/{created['id']}", json=update)
    assert resp.status_code == 200
    # api_key was set originally, so api_key_set must still be True
    assert resp.json()["config"]["api_key_set"] is True


def test_update_connector_nsfw_true(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    update = {
        "name": "My Ollama",
        "type": "text",
        "backend": "openai_api",
        "config": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3",
            "nsfw": True,
        },
    }
    resp = client.put(f"/api/connectors/{created['id']}", json=update)
    assert resp.status_code == 200
    assert resp.json()["config"]["nsfw"] is True


def test_update_clears_api_key_if_empty_string(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    update = {
        "name": "My Ollama",
        "type": "text",
        "backend": "openai_api",
        "config": {
            "base_url": "http://localhost:11434/v1",
            "model": "llama3",
            "api_key": "",  # explicit empty → clear
        },
    }
    resp = client.put(f"/api/connectors/{created['id']}", json=update)
    assert resp.status_code == 200
    assert resp.json()["config"]["api_key_set"] is False


def test_update_connector_not_found(client):
    resp = client.put("/api/connectors/nonexistent", json=TEXT_PAYLOAD)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/connectors/{id}
# ---------------------------------------------------------------------------


def test_delete_connector(client):
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    resp = client.delete(f"/api/connectors/{created['id']}")
    assert resp.status_code == 204
    resp2 = client.get(f"/api/connectors/{created['id']}")
    assert resp2.status_code == 404


def test_delete_connector_not_found(client):
    resp = client.delete("/api/connectors/nonexistent")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/connectors/{id}/activate
# ---------------------------------------------------------------------------


def test_activate_connector(client_with_manager):
    client, manager = client_with_manager
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    resp = client.post(f"/api/connectors/{created['id']}/activate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_active"] is True
    assert data["id"] == created["id"]
    # Verify is_active reflected in GET
    detail = client.get(f"/api/connectors/{created['id']}").json()
    assert detail["is_active"] is True


def test_activate_not_found(client):
    resp = client.post("/api/connectors/nonexistent/activate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/connectors/{id}/test
# ---------------------------------------------------------------------------


def test_test_connector_not_found(client):
    resp = client.post("/api/connectors/nonexistent/test")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/connectors/backends
# ---------------------------------------------------------------------------


def test_list_backends(client):
    resp = client.get("/api/connectors/backends")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    ids = [b["id"] for b in data]
    assert "openai_api" in ids


def test_openai_backend_schema_has_common_and_per_type_fields(client):
    resp = client.get("/api/connectors/backends")
    assert resp.status_code == 200
    data = resp.json()
    openai = next(b for b in data if b["id"] == "openai_api")
    schema = openai["config_schema"]
    assert "common" in schema
    assert "by_type" in schema
    assert "timeout" in schema["common"]
    assert "nsfw" in schema["common"]
    assert "max_tokens" in schema["by_type"]["text"]
    assert "temperature" in schema["by_type"]["text"]
    assert "size" in schema["by_type"]["image"]


def test_comfyui_backend_schema_has_common_and_per_type_fields(client):
    resp = client.get("/api/connectors/backends")
    assert resp.status_code == 200
    data = resp.json()
    comfy = next(b for b in data if b["id"] == "comfyui")
    schema = comfy["config_schema"]
    assert "common" in schema
    assert "by_type" in schema
    assert "base_url" in schema["common"]
    assert "timeout" in schema["common"]
    assert "nsfw" in schema["common"]
    assert "workflow" in schema["by_type"]["image"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_list_redacts_api_key(client):
    client.post("/api/connectors/", json=TEXT_PAYLOAD)
    connectors = client.get("/api/connectors/").json()
    for c in connectors:
        assert "api_key" not in c["config"]
        assert "api_key_set" in c["config"]


# ---------------------------------------------------------------------------
# DELETE active connector clears active_connectors in config
# ---------------------------------------------------------------------------


def test_delete_active_connector_clears_active(client_with_manager):
    client, manager = client_with_manager
    created = client.post("/api/connectors/", json=TEXT_PAYLOAD).json()
    client.post(f"/api/connectors/{created['id']}/activate")
    assert manager.is_active(created["id"])
    client.delete(f"/api/connectors/{created['id']}")
    assert manager._config.active_connectors.text == ""


def test_backends_expose_image_support_schema(client):
    backends = client.get("/api/connectors/backends").json()
    openai = next(b for b in backends if b["id"] == "openai_api")
    field = openai["config_schema"]["by_type"]["image"]["image_support"]
    assert field["type"] == "boolean"
