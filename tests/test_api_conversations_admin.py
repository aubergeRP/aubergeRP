from __future__ import annotations

from fastapi.testclient import TestClient

from aubergeRP.config import get_config, reset_config
from aubergeRP.main import create_app
from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService


def _bootstrap(tmp_path):
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    char = char_svc.create_character(CharacterData(name="Mina", description="Mage", first_mes="Hello"))
    conv_a = conv_svc.create_conversation(char.id, owner="session-1")
    conv_b = conv_svc.create_conversation(char.id, owner="session-2")
    return conv_svc, conv_a, conv_b


def test_admin_list_returns_conversations_of_every_owner(tmp_path):
    _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/conversations/admin/all")

    assert resp.status_code == 200
    owners = {c["owner"] for c in resp.json()}
    assert owners == {"session-1", "session-2"}


def test_admin_inject_message_appends_to_history(tmp_path):
    conv_svc, conv_a, _ = _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.post(
            f"/api/conversations/admin/{conv_a.id}/messages",
            json={"role": "user", "content": "injected line"},
        )

    assert resp.status_code == 201
    assert resp.json()["content"] == "injected line"
    messages = conv_svc.get_conversation(conv_a.id).messages
    assert [m.content for m in messages] == ["Hello", "injected line"]


def test_admin_clear_history_keeps_conversation(tmp_path):
    conv_svc, conv_a, _ = _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.delete(f"/api/conversations/admin/{conv_a.id}/messages")

    assert resp.status_code == 204
    assert conv_svc.get_conversation(conv_a.id).messages == []


def test_admin_delete_conversation_of_another_owner(tmp_path):
    conv_svc, conv_a, conv_b = _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.delete(f"/api/conversations/admin/{conv_b.id}")

    assert resp.status_code == 204
    remaining = {c.id for c in conv_svc.list_conversations()}
    assert remaining == {conv_a.id}


def test_admin_endpoints_return_404_for_unknown_conversation(tmp_path):
    _bootstrap(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        assert client.delete("/api/conversations/admin/nope").status_code == 404
        assert client.delete("/api/conversations/admin/nope/messages").status_code == 404
        assert client.post(
            "/api/conversations/admin/nope/messages",
            json={"role": "assistant", "content": "x"},
        ).status_code == 404
