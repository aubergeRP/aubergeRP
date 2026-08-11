import json
from pathlib import Path

import pytest
import yaml

from aubergeRP.config import Config
from aubergeRP.connectors.manager import ConnectorManager
from aubergeRP.connectors.openai_image import OpenAIImageConnector
from aubergeRP.connectors.openai_text import OpenAITextConnector
from aubergeRP.models.connector import ConnectorCreate, ConnectorUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_manager(tmp_path: Path, active_text: str = "", active_image: str = "") -> ConnectorManager:
    config = Config()
    config.active_connectors.text = active_text
    config.active_connectors.image = active_image
    config_file = tmp_path / "config.yaml"
    return ConnectorManager(data_dir=tmp_path, config=config, config_path=config_file)


def text_create(**overrides) -> ConnectorCreate:
    defaults = dict(
        name="My Ollama",
        type="text",
        backend="openai_api",
        config={"base_url": "http://localhost:11434/v1", "model": "llama3", "api_key": ""},
    )
    defaults.update(overrides)
    return ConnectorCreate(**defaults)


def image_create(**overrides) -> ConnectorCreate:
    defaults = dict(
        name="My Image Gen",
        type="image",
        backend="openai_api",
        config={"base_url": "https://openrouter.ai/api/v1", "model": "gemini-flash", "api_key": "sk-or-x"},
    )
    defaults.update(overrides)
    return ConnectorCreate(**defaults)


# ---------------------------------------------------------------------------
# Init & loading
# ---------------------------------------------------------------------------


def test_empty_data_dir(tmp_path):
    mgr = make_manager(tmp_path)
    assert mgr.list_connectors() == []


def test_loads_existing_connectors(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    # New manager loading from same dir
    mgr2 = make_manager(tmp_path)
    assert len(mgr2.list_connectors()) == 1
    assert mgr2.list_connectors()[0].id == c.id


def test_skips_malformed_json(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.create_connector(text_create())
    (tmp_path / "connectors" / "bad.json").write_text("not json")
    mgr2 = make_manager(tmp_path)
    assert len(mgr2.list_connectors()) == 1


# ---------------------------------------------------------------------------
# CRUD — create
# ---------------------------------------------------------------------------


def test_create_returns_instance_with_id(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    assert c.id
    assert c.name == "My Ollama"
    assert c.type == "text"


def test_create_persists_to_disk(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    path = tmp_path / "connectors" / f"{c.id}.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["id"] == c.id


def test_create_multiple(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.create_connector(text_create())
    mgr.create_connector(image_create())
    assert len(mgr.list_connectors()) == 2


# ---------------------------------------------------------------------------
# CRUD — get / list
# ---------------------------------------------------------------------------


def test_get_connector(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    assert mgr.get_connector(c.id).id == c.id


def test_get_connector_not_found(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(KeyError):
        mgr.get_connector("nonexistent-id")


def test_list_filter_by_type(tmp_path):
    mgr = make_manager(tmp_path)
    mgr.create_connector(text_create())
    mgr.create_connector(image_create())
    text_list = mgr.list_connectors(connector_type="text")
    image_list = mgr.list_connectors(connector_type="image")
    assert len(text_list) == 1 and text_list[0].type == "text"
    assert len(image_list) == 1 and image_list[0].type == "image"


# ---------------------------------------------------------------------------
# CRUD — update
# ---------------------------------------------------------------------------


def test_update_name_and_model(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    upd = ConnectorUpdate(
        name="Updated Ollama",
        type="text",
        backend="openai_api",
        config={"base_url": "http://localhost:11434/v1", "model": "phi3"},
    )
    updated = mgr.update_connector(c.id, upd)
    assert updated.name == "Updated Ollama"
    assert updated.config["model"] == "phi3"


def test_update_preserves_api_key_when_omitted(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(image_create())
    # Update without api_key in config
    upd = ConnectorUpdate(
        name="Updated",
        type="image",
        backend="openai_api",
        config={"base_url": "https://openrouter.ai/api/v1", "model": "new-model"},
    )
    updated = mgr.update_connector(c.id, upd)
    assert updated.config["api_key"] == "sk-or-x"


def test_update_clears_api_key_when_empty_string(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(image_create())
    upd = ConnectorUpdate(
        name="Updated",
        type="image",
        backend="openai_api",
        config={"base_url": "https://openrouter.ai/api/v1", "model": "x", "api_key": ""},
    )
    updated = mgr.update_connector(c.id, upd)
    assert updated.config["api_key"] == ""


def test_update_persists_to_disk(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    upd = ConnectorUpdate(
        name="Disk Check",
        type="text",
        backend="openai_api",
        config={"model": "llama3"},
    )
    mgr.update_connector(c.id, upd)
    data = json.loads((tmp_path / "connectors" / f"{c.id}.json").read_text())
    assert data["name"] == "Disk Check"


def test_update_not_found(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(KeyError):
        mgr.update_connector("bad-id", ConnectorUpdate(
            name="X", type="text", backend="openai_api", config={}
        ))


# ---------------------------------------------------------------------------
# CRUD — delete
# ---------------------------------------------------------------------------


def test_delete_removes_from_memory_and_disk(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    path = tmp_path / "connectors" / f"{c.id}.json"
    assert path.exists()
    mgr.delete_connector(c.id)
    assert not path.exists()
    with pytest.raises(KeyError):
        mgr.get_connector(c.id)


def test_delete_clears_active_text_in_config(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    config_file = tmp_path / "config.yaml"
    mgr._config.active_connectors.text = c.id
    mgr.delete_connector(c.id)
    assert mgr._config.active_connectors.text == ""
    saved = yaml.safe_load(config_file.read_text())
    assert saved["active_connectors"]["text"] == ""


def test_delete_clears_active_image_in_config(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(image_create())
    config_file = tmp_path / "config.yaml"
    mgr._config.active_connectors.image = c.id
    mgr.delete_connector(c.id)
    assert mgr._config.active_connectors.image == ""
    saved = yaml.safe_load(config_file.read_text())
    assert saved["active_connectors"]["image"] == ""


def test_delete_not_found(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(KeyError):
        mgr.delete_connector("no-such-id")


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def test_set_active_text(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    config_file = tmp_path / "config.yaml"
    mgr.set_active(c.id)
    assert mgr._config.active_connectors.text == c.id
    saved = yaml.safe_load(config_file.read_text())
    assert saved["active_connectors"]["text"] == c.id


def test_set_active_image(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(image_create())
    mgr.set_active(c.id)
    assert mgr._config.active_connectors.image == c.id


def test_is_active_true(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    mgr.set_active(c.id)
    assert mgr.is_active(c.id) is True


def test_is_active_false(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    assert mgr.is_active(c.id) is False


def test_set_active_not_found(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(KeyError):
        mgr.set_active("bad-id")


# ---------------------------------------------------------------------------
# Active connector instantiation
# ---------------------------------------------------------------------------


def test_get_active_text_connector_none_when_not_set(tmp_path):
    mgr = make_manager(tmp_path)
    assert mgr.get_active_text_connector() is None


def test_get_active_text_connector_returns_instance(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create())
    mgr.set_active(c.id)
    conn = mgr.get_active_text_connector()
    assert isinstance(conn, OpenAITextConnector)
    assert conn.config.model == "llama3"


def test_get_active_image_connector_returns_instance(tmp_path):
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(image_create())
    mgr.set_active(c.id)
    conn = mgr.get_active_image_connector()
    assert isinstance(conn, OpenAIImageConnector)
    assert conn.config.model == "gemini-flash"


def test_get_active_text_connector_none_if_id_stale(tmp_path):
    mgr = make_manager(tmp_path, active_text="deleted-id")
    assert mgr.get_active_text_connector() is None


def test_get_active_image_connector_none_if_id_stale(tmp_path):
    mgr = make_manager(tmp_path, active_image="deleted-id")
    assert mgr.get_active_image_connector() is None


# ---------------------------------------------------------------------------
# Per-task text roles (multi-model support)
# ---------------------------------------------------------------------------


def test_text_role_falls_back_to_main_connector(tmp_path):
    mgr = make_manager(tmp_path)
    main = mgr.create_connector(text_create())
    mgr.set_active(main.id)

    for role in ("text_summarization", "text_utility"):
        assert mgr.get_active_id_for_type(role) == main.id
        conn = mgr.get_text_connector(role)
        assert isinstance(conn, OpenAITextConnector)
        assert conn.config.model == "llama3"


def test_text_role_uses_override_when_set(tmp_path):
    mgr = make_manager(tmp_path)
    main = mgr.create_connector(text_create())
    small = mgr.create_connector(text_create(
        name="Small local",
        config={"base_url": "http://localhost:11434/v1", "model": "qwen-mini", "api_key": ""},
    ))
    mgr.set_active(main.id)
    mgr._config.active_connectors.text_summarization = small.id

    assert mgr.get_text_connector("text_summarization").config.model == "qwen-mini"
    # Other roles are unaffected.
    assert mgr.get_text_connector("text_utility").config.model == "llama3"
    assert mgr.get_active_text_connector().config.model == "llama3"


def test_unknown_text_role_raises(tmp_path):
    mgr = make_manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.get_text_connector("text_nope")


def test_delete_connector_clears_role_overrides(tmp_path):
    mgr = make_manager(tmp_path)
    main = mgr.create_connector(text_create())
    small = mgr.create_connector(text_create(name="Small local"))
    mgr.set_active(main.id)
    mgr._config.active_connectors.text_summarization = small.id
    mgr._config.active_connectors.text_utility = small.id

    mgr.delete_connector(small.id)

    assert mgr._config.active_connectors.text_summarization == ""
    assert mgr._config.active_connectors.text_utility == ""
    # The main connector still answers for every role.
    assert mgr.get_text_connector("text_utility").config.model == "llama3"


def test_get_active_text_connector_with_empty_string_extra_body(tmp_path):
    # Regression: connector JSON stored extra_body="" (empty string from admin form)
    # instead of {}; _build_connector must not fail with a ValidationError.
    mgr = make_manager(tmp_path)
    c = mgr.create_connector(text_create(config={
        "base_url": "http://localhost:11434/v1",
        "model": "llama3",
        "api_key": "",
        "extra_body": "",
    }))
    mgr.set_active(c.id)
    conn = mgr.get_active_text_connector()
    assert isinstance(conn, OpenAITextConnector)
    assert conn.config.extra_body == {}
