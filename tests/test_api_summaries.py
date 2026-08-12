"""Admin API for inspecting and driving conversation summaries."""
from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aubergeRP.config import get_config, reset_config
from aubergeRP.main import create_app
from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.summary_service import SummaryService


class _SummarizingConnector:
    connector_type = "text"
    supports_tool_calling = False

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        yield "RELATIONSHIP: allies met at the inn."


def _setup(tmp_path, *, turns: int = 4):
    reset_config()
    config = get_config()
    config.app.data_dir = str(tmp_path)
    config.chat.context_window = 400
    config.chat.summarization_threshold = 0.75

    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    char = char_svc.create_character(CharacterData(name="Aria", description="A healer."))
    conv = conv_svc.create_conversation(char.id)
    for i in range(turns):
        conv_svc.append_message(conv.id, "user", f"user-{i} " + "x" * 200)
        conv_svc.append_message(conv.id, "assistant", f"assistant-{i} " + "y" * 200)
    return conv_svc, conv


def _patched_manager():
    manager = MagicMock()
    manager.get_text_connector.return_value = _SummarizingConnector()
    manager.get_active_text_connector.return_value = _SummarizingConnector()
    return patch("aubergeRP.connectors.manager.ConnectorManager", return_value=manager)


def test_list_reports_context_size_and_budget(tmp_path):
    _conv_svc, conv = _setup(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/summaries/")

    assert resp.status_code == 200
    rows = resp.json()
    row = next(r for r in rows if r["conversation_id"] == conv.id)
    assert row["message_count"] == 8
    assert row["messages_since_summary"] == 8
    assert row["context_tokens"] > 0
    assert row["budget_tokens"] == int(400 * 0.75) - 256
    assert row["summary_count"] == 0
    assert row["last_summary_at"] is None


def test_detail_lists_summary_and_following_messages(tmp_path):
    _conv_svc, conv = _setup(tmp_path)

    app = create_app()
    with TestClient(app) as client:
        with _patched_manager():
            created = client.post(f"/api/summaries/{conv.id}/summarize")
            assert created.status_code == 200
            assert created.json()["created"] is True

        resp = client.get(f"/api/summaries/{conv.id}")

    assert resp.status_code == 200
    data = resp.json()
    assert "allies" in data["summary"]
    assert len(data["summaries"]) == 1
    # Only the recent messages are still sent verbatim.
    assert 0 < len(data["messages_since"]) < 8
    assert data["messages_since"][-1]["content"].startswith("assistant-3")


def test_detail_unknown_conversation_is_404(tmp_path):
    _setup(tmp_path)
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/summaries/does-not-exist").status_code == 404


def test_delete_removes_last_summary_then_all(tmp_path):
    conv_svc, conv = _setup(tmp_path)
    app = create_app()
    with TestClient(app) as client:
        with _patched_manager():
            client.post(f"/api/summaries/{conv.id}/summarize")
            # New turns are needed before a second summary has anything to chew on.
            for i in range(4):
                conv_svc.append_message(conv.id, "user", f"later-{i} " + "z" * 200)
            client.post(f"/api/summaries/{conv.id}/summarize")

        assert len(SummaryService(tmp_path).list_chain(conv.id)) == 2

        assert client.delete(f"/api/summaries/{conv.id}").json() == {"deleted": 1}
        assert client.delete(f"/api/summaries/{conv.id}?all=true").json() == {"deleted": 1}

    assert SummaryService(tmp_path).get_latest(conv.id) is None


def test_summarize_reports_when_there_is_nothing_to_do(tmp_path):
    _conv_svc, conv = _setup(tmp_path, turns=1)  # 2 messages < _MIN_RECENT_MESSAGES
    app = create_app()
    with TestClient(app) as client, _patched_manager():
        resp = client.post(f"/api/summaries/{conv.id}/summarize")
    assert resp.status_code == 200
    assert resp.json() == {"created": False}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/summaries/"),
        ("get", "/api/summaries/whatever"),
        ("post", "/api/summaries/whatever/summarize"),
        ("delete", "/api/summaries/whatever"),
    ],
)
def test_endpoints_require_admin(tmp_path, monkeypatch, method, path):
    _setup(tmp_path)
    monkeypatch.delenv("AUBERGE_DISABLE_ADMIN_AUTH", raising=False)
    get_config().app.admin_jwt_secret = "s" * 32

    app = create_app()
    with TestClient(app) as client:
        resp = getattr(client, method)(path)
    assert resp.status_code == 401
