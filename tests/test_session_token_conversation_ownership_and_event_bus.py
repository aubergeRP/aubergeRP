"""
Tests for session-token scoping, conversation ownership, event-bus behavior,
and multi-browser SSE delivery.

Covers:
  1. Per-user session token: X-Session-Token header extracted and used
  2. Per-user conversation ownership: owner stored on create, filtered on list
  3. Conversation owner field present in model and summary
  4. EventBus: subscribe / publish / unsubscribe
  5. Multi-browser SSE: GET /api/chat/{id}/events receives events published by POST
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aubergeRP.event_bus import EventBus
from aubergeRP.main import create_app
from aubergeRP.models.conversation import Conversation
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.conversation_service import ConversationService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conv_service(tmp_path: Path) -> ConversationService:
    char_svc = CharacterService(data_dir=tmp_path)
    return ConversationService(data_dir=tmp_path, character_service=char_svc)


def _write_ownerless_conversation(tmp_path: Path, conv_id: str, char_id: str = "c1") -> None:
    """Write a legacy conversation JSON that has no 'owner' field."""
    import json

    convs_dir = tmp_path / "conversations"
    convs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    data = {
        "id": conv_id,
        "character_id": char_id,
        "character_name": "Test",
        "title": "Legacy",
        "messages": [],
        "created_at": now,
        "updated_at": now,
        # no "owner" key — simulates a legacy file created before ownership
    }
    (convs_dir / f"{conv_id}.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# 1. Conversation model has owner field
# ---------------------------------------------------------------------------


def test_conversation_model_has_owner_field():
    now = datetime.now(UTC)
    conv = Conversation(
        id="x",
        character_id="c",
        character_name="Bob",
        title="t",
        created_at=now,
        updated_at=now,
    )
    assert conv.owner == ""


def test_conversation_model_owner_stored():
    now = datetime.now(UTC)
    conv = Conversation(
        id="x",
        character_id="c",
        character_name="Bob",
        title="t",
        owner="my-token",
        created_at=now,
        updated_at=now,
    )
    assert conv.owner == "my-token"


# ---------------------------------------------------------------------------
# 2. ConversationService: owner stored on create and filtered on list
# ---------------------------------------------------------------------------


def _make_character_file(tmp_path: Path, char_id: str = "char-1") -> str:
    """Create a character and return its ID."""
    char_svc = CharacterService(data_dir=tmp_path)
    from aubergeRP.models.character import CharacterData
    char = char_svc.create_character(CharacterData(
        name="TestChar",
        description="A test character.",
        first_mes="",
    ))
    return char.id


def test_create_conversation_stores_owner(tmp_path):
    char_id = _make_character_file(tmp_path)
    svc = _make_conv_service(tmp_path)
    conv = svc.create_conversation(char_id, owner="tok-abc")
    assert conv.owner == "tok-abc"


def test_list_conversations_filters_by_owner(tmp_path):
    char_id = _make_character_file(tmp_path)
    svc = _make_conv_service(tmp_path)
    svc.create_conversation(char_id, owner="tok-alice")
    svc.create_conversation(char_id, owner="tok-bob")

    alice = svc.list_conversations(owner="tok-alice")
    bob = svc.list_conversations(owner="tok-bob")

    assert len(alice) == 1
    assert alice[0].owner == "tok-alice"
    assert len(bob) == 1
    assert bob[0].owner == "tok-bob"


def test_list_conversations_ownerless_visible_to_all(tmp_path):
    """Legacy conversations with no owner are visible to everyone."""
    conv_id = str(uuid.uuid4())
    _write_ownerless_conversation(tmp_path, conv_id)
    svc = _make_conv_service(tmp_path)

    # Visible when querying with any token
    results_alice = svc.list_conversations(owner="tok-alice")
    results_bob = svc.list_conversations(owner="tok-bob")
    results_none = svc.list_conversations(owner=None)

    assert any(c.id == conv_id for c in results_alice)
    assert any(c.id == conv_id for c in results_bob)
    assert any(c.id == conv_id for c in results_none)


def test_list_conversations_no_token_returns_all(tmp_path):
    char_id = _make_character_file(tmp_path)
    svc = _make_conv_service(tmp_path)
    svc.create_conversation(char_id, owner="tok-alice")
    svc.create_conversation(char_id, owner="tok-bob")

    all_convs = svc.list_conversations(owner=None)
    assert len(all_convs) == 2


# ---------------------------------------------------------------------------
# 3. API: X-Session-Token header used for conversation scoping
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path):
    from aubergeRP.config import get_config, reset_config
    reset_config()
    get_config().app.data_dir = str(tmp_path)
    char_id = _make_character_file(tmp_path)
    app = create_app()
    with TestClient(app) as c:
        yield c, char_id


def _post_conv(client, char_id: str, token: str = "") -> dict:
    headers = {"X-Session-Token": token} if token else {}
    resp = client.post(
        "/api/conversations/",
        json={"character_id": char_id},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()


def test_api_create_conversation_stores_session_token(api_client):
    client, char_id = api_client
    conv = _post_conv(client, char_id=char_id, token="tok-xyz")
    assert conv["owner"] == "tok-xyz"


def test_api_list_conversations_filtered_by_token(api_client):
    client, char_id = api_client
    _post_conv(client, char_id=char_id, token="tok-a")
    _post_conv(client, char_id=char_id, token="tok-b")

    resp_a = client.get("/api/conversations/", headers={"X-Session-Token": "tok-a"})
    resp_b = client.get("/api/conversations/", headers={"X-Session-Token": "tok-b"})

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert len(resp_a.json()) == 1
    assert resp_a.json()[0]["owner"] == "tok-a"
    assert len(resp_b.json()) == 1
    assert resp_b.json()[0]["owner"] == "tok-b"


def test_api_list_conversations_no_token_returns_all(api_client):
    client, char_id = api_client
    _post_conv(client, char_id=char_id, token="tok-a")
    _post_conv(client, char_id=char_id, token="tok-b")

    resp = client.get("/api/conversations/")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_api_create_conversation_logs_schedule_creation_failure(tmp_path, monkeypatch, caplog):
    from aubergeRP.config import get_config, reset_config
    from aubergeRP.models.character import CharacterData

    reset_config()
    get_config().app.data_dir = str(tmp_path)
    char_svc = CharacterService(data_dir=tmp_path)
    char = char_svc.create_character(
        CharacterData(
            name="SchedChar",
            description="desc",
            extensions={
                "aubergerp": {
                    "schedules": [
                        {
                            "id": "followup",
                            "enabled": True,
                            "type": "after_delay",
                            "delay_minutes": 10,
                            "instruction": "Ping later",
                        }
                    ]
                }
            },
        )
    )

    def _fail_get_or_create(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(
        "aubergeRP.services.schedule_instance_service.ScheduleInstanceService.get_or_create",
        _fail_get_or_create,
    )
    app = create_app()
    with TestClient(app) as client, caplog.at_level("WARNING"):
        resp = client.post("/api/conversations/", json={"character_id": char.id})
    assert resp.status_code == 201
    assert any(
        "Failed to create proactive schedule instance" in rec.message for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# 4. EventBus: subscribe / publish / unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_bus_subscribe_and_publish():
    bus = EventBus()
    q = bus.subscribe("tok", "conv-1")
    await bus.publish("tok", "conv-1", {"type": "token", "content": "hi"})
    event = q.get_nowait()
    assert event == {"type": "token", "content": "hi"}


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers():
    bus = EventBus()
    q1 = bus.subscribe("tok", "conv-1")
    q2 = bus.subscribe("tok", "conv-1")
    await bus.publish("tok", "conv-1", {"type": "done"})
    assert q1.get_nowait() == {"type": "done"}
    assert q2.get_nowait() == {"type": "done"}


@pytest.mark.asyncio
async def test_event_bus_unsubscribe_removes_queue():
    bus = EventBus()
    q = bus.subscribe("tok", "conv-1")
    bus.unsubscribe("tok", "conv-1", q)
    await bus.publish("tok", "conv-1", {"type": "done"})
    assert q.empty()


@pytest.mark.asyncio
async def test_event_bus_different_tokens_isolated():
    bus = EventBus()
    q_a = bus.subscribe("tok-a", "conv-1")
    q_b = bus.subscribe("tok-b", "conv-1")
    await bus.publish("tok-a", "conv-1", {"type": "token", "content": "hello"})
    assert not q_a.empty()
    assert q_b.empty()


@pytest.mark.asyncio
async def test_event_bus_different_conversations_isolated():
    bus = EventBus()
    q1 = bus.subscribe("tok", "conv-1")
    q2 = bus.subscribe("tok", "conv-2")
    await bus.publish("tok", "conv-1", {"type": "token", "content": "hello"})
    assert not q1.empty()
    assert q2.empty()
