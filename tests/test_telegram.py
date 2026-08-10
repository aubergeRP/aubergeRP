"""Tests for Telegram bot admin CRUD, token security, and runtime behaviour."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aubergeRP.main import create_app
from aubergeRP.services.channel_session_service import ChannelSessionService
from aubergeRP.services.telegram_bot_service import (
    TelegramBotCreate,
    TelegramBotService,
    TelegramBotUpdate,
)
from aubergeRP.services.telegram_runtime_manager import TelegramRuntimeManager, split_message

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def app(tmp_path):
    import os
    os.environ["AUBERGE_DATA_DIR"] = str(tmp_path / "_appdata")
    return create_app()


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def tg_svc(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    return TelegramBotService(data_dir=data_dir)


@pytest.fixture()
def cs_svc(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    return ChannelSessionService(data_dir=data_dir)


def _make_character(tmp_path) -> str:
    """Seed a character and return its ID."""
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    init_db(data_dir)
    svc = CharacterService(data_dir=data_dir)
    card = svc.create_character(CharacterData(name="Alice", description="Test character"))
    return card.id


# ── Admin CRUD tests ──────────────────────────────────────────────────────────


def test_list_bots_empty(client):
    resp = client.get("/api/telegram/bots/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_bot(client, tmp_path):
    # We need a character first; use the client's app data_dir
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    svc = CharacterService(data_dir=data_dir)
    char = svc.create_character(CharacterData(name="Alice", description="Test"))

    resp = client.post("/api/telegram/bots/", json={
        "name": "Alice Bot",
        "token": "secret_token_123",
        "character_id": char.id,
        "enabled": False,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Alice Bot"
    assert data["character_id"] == char.id
    # Token must NOT appear in response
    assert "token" not in data
    assert "secret_token_123" not in str(data)


def test_token_never_returned_in_list(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )

    client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "supersecret",
        "character_id": char.id,
        "enabled": False,
    })
    resp = client.get("/api/telegram/bots/")
    assert "supersecret" not in resp.text


def test_get_bot_no_token(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    created = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "mysecrettoken",
        "character_id": char.id,
        "enabled": False,
    }).json()
    resp = client.get(f"/api/telegram/bots/{created['id']}")
    assert resp.status_code == 200
    assert "mysecrettoken" not in resp.text


def test_edit_without_new_token_preserves_token(tg_svc):
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Bot",
        token="original_token",
        character_id="char1",
    ))
    # Update without token
    tg_svc.update_bot(bot.id, TelegramBotUpdate(name="Bot Renamed"))
    # Read back token directly (internal)
    token = tg_svc.get_bot_token(bot.id)
    assert token == "original_token"


def test_edit_with_new_token_replaces_token(tg_svc):
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Bot",
        token="original_token",
        character_id="char1",
    ))
    tg_svc.update_bot(bot.id, TelegramBotUpdate(token="new_token"))
    assert tg_svc.get_bot_token(bot.id) == "new_token"


def test_delete_bot(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    bot_id = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "tok",
        "character_id": char.id,
        "enabled": False,
    }).json()["id"]

    resp = client.delete(f"/api/telegram/bots/{bot_id}")
    assert resp.status_code == 204

    resp = client.get(f"/api/telegram/bots/{bot_id}")
    assert resp.status_code == 404


def test_enable_disable_bot(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    bot = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "tok",
        "character_id": char.id,
        "enabled": False,
    }).json()

    resp = client.post(f"/api/telegram/bots/{bot['id']}/enable")
    assert resp.json()["enabled"] is True

    resp = client.post(f"/api/telegram/bots/{bot['id']}/disable")
    assert resp.json()["enabled"] is False


def test_get_nonexistent_bot_returns_404(client):
    resp = client.get("/api/telegram/bots/nonexistent")
    assert resp.status_code == 404


# ── Test connection (mocked Telegram) ────────────────────────────────────────


def test_test_connection_success(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    bot = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "tok",
        "character_id": char.id,
        "enabled": False,
    }).json()

    me_mock = MagicMock()
    me_mock.id = 123456
    me_mock.username = "alice_rp_bot"
    bot_mock = AsyncMock()
    bot_mock.get_me = AsyncMock(return_value=me_mock)
    bot_mock.session = AsyncMock()

    with patch("aubergeRP.routers.telegram.Bot", return_value=bot_mock):
        resp = client.post(f"/api/telegram/bots/{bot['id']}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["telegram_username"] == "alice_rp_bot"
    assert data["last_error"] == ""


def test_test_connection_failure(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    bot = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "bad_token",
        "character_id": char.id,
        "enabled": False,
    }).json()

    with patch("aubergeRP.routers.telegram.Bot", side_effect=Exception("Unauthorized")):
        resp = client.post(f"/api/telegram/bots/{bot['id']}/test")

    assert resp.status_code == 200
    data = resp.json()
    assert data["last_error"] != ""
    assert "Unauthorized" in data["last_error"]


# ── Channel session isolation ─────────────────────────────────────────────────


def test_same_user_different_bots_get_separate_conversations(cs_svc, tmp_path):
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = Path(cs_svc._data_dir)
    char_svc = CharacterService(data_dir=data_dir)
    char = char_svc.create_character(CharacterData(name="Alice", description="Test"))

    conv1, created1 = cs_svc.get_or_create(
        channel="telegram",
        channel_instance_id="bot-1",
        external_user_id="user-42",
        external_chat_id="42",
        character_id=char.id,
    )
    conv2, created2 = cs_svc.get_or_create(
        channel="telegram",
        channel_instance_id="bot-2",
        external_user_id="user-42",
        external_chat_id="42",
        character_id=char.id,
    )
    assert conv1 != conv2
    assert created1 is True
    assert created2 is True


def test_different_users_same_bot_get_separate_conversations(cs_svc, tmp_path):
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = Path(cs_svc._data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )

    conv1, _ = cs_svc.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    conv2, _ = cs_svc.get_or_create("telegram", "bot-1", "user-2", "2", char.id)
    assert conv1 != conv2


def test_session_reuse_on_repeat_call(cs_svc, tmp_path):
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = Path(cs_svc._data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )

    conv1, created1 = cs_svc.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    conv2, created2 = cs_svc.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    assert conv1 == conv2
    assert created1 is True
    assert created2 is False


def test_session_survives_restart(tmp_path):
    """A new ChannelSessionService instance reuses the same DB."""
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )

    svc1 = ChannelSessionService(data_dir=data_dir)
    conv_id, _ = svc1.get_or_create("telegram", "bot-1", "user-1", "1", char.id)

    # Simulate restart by creating fresh instance (same DB file)
    from aubergeRP.database import reset_engine
    reset_engine()
    init_db(data_dir)
    svc2 = ChannelSessionService(data_dir=data_dir)
    conv_id2, created = svc2.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    assert conv_id2 == conv_id
    assert created is False


# ── Reset ─────────────────────────────────────────────────────────────────────


def test_reset_creates_new_conversation(cs_svc, tmp_path):
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = Path(cs_svc._data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )

    conv1, _ = cs_svc.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    conv2 = cs_svc.reset("telegram", "bot-1", "user-1", "1", char.id)
    assert conv2 != conv1

    # Subsequent get should return the new conversation
    conv3, created = cs_svc.get_or_create("telegram", "bot-1", "user-1", "1", char.id)
    assert conv3 == conv2
    assert created is False


# ── Message splitting ─────────────────────────────────────────────────────────


def test_split_message_short():
    assert split_message("hello") == ["hello"]


def test_split_message_at_paragraph_boundary():
    long_text = "A" * 3000 + "\n\n" + "B" * 2000
    parts = split_message(long_text)
    assert len(parts) == 2
    assert "A" * 3000 in parts[0]
    assert "B" in parts[1]


def test_split_message_hard_split_when_no_boundary():
    long_text = "X" * 10000
    parts = split_message(long_text)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == long_text


def test_split_message_exact_limit():
    text = "X" * 4096
    assert split_message(text) == [text]


def test_split_message_one_over_limit():
    text = "X" * 4097
    parts = split_message(text)
    assert len(parts) >= 2
    assert "".join(parts) == text


# ── Runtime manager ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_bots_can_run_independently(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)

    started: list[str] = []

    async def fake_run(bot_id, token, character_id, dialogue_only=False):
        started.append(bot_id)
        await asyncio.sleep(0.1)

    with patch.object(mgr, "_run_bot", side_effect=fake_run):
        await mgr.start_bot("bot-1", "tok1", "char1")
        await mgr.start_bot("bot-2", "tok2", "char2")
        await asyncio.sleep(0.01)
        assert mgr.is_running("bot-1")
        assert mgr.is_running("bot-2")
        await mgr.stop_all()
        assert "bot-1" in started
        assert "bot-2" in started


@pytest.mark.asyncio
async def test_disabling_one_bot_does_not_stop_others(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)

    async def fake_run(bot_id, token, character_id, dialogue_only=False):
        await asyncio.sleep(10)

    with patch.object(mgr, "_run_bot", side_effect=fake_run):
        await mgr.start_bot("bot-1", "tok1", "char1")
        await mgr.start_bot("bot-2", "tok2", "char2")
        await asyncio.sleep(0.01)
        assert mgr.is_running("bot-1")
        assert mgr.is_running("bot-2")

        await mgr.stop_bot("bot-1")
        assert not mgr.is_running("bot-1")
        assert mgr.is_running("bot-2")
        await mgr.stop_all()


# ── Character delete protection ───────────────────────────────────────────────


def test_delete_character_blocked_if_referenced_by_telegram_bot(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )
    client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "tok",
        "character_id": char.id,
        "enabled": False,
    })
    resp = client.delete(f"/api/characters/{char.id}")
    assert resp.status_code == 409


def test_delete_character_succeeds_if_not_referenced(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )
    resp = client.delete(f"/api/characters/{char.id}")
    assert resp.status_code == 204


# ── Per-session concurrency ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_session_lock_is_deterministic(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    mgr = TelegramRuntimeManager(data_dir=data_dir)

    order: list[str] = []

    async def slow_gen(n: str) -> str:
        lock = mgr._get_conv_lock("conv-1")
        async with lock:
            order.append(f"start-{n}")
            await asyncio.sleep(0.02)
            order.append(f"end-{n}")
        return "ok"

    await asyncio.gather(slow_gen("A"), slow_gen("B"))
    # They must not interleave
    assert order[0].startswith("start")
    assert order[1].startswith("end")
    assert order[2].startswith("start")
    assert order[3].startswith("end")


# ── /status hides secrets ────────────────────────────────────────────────────


def test_status_endpoint_hides_secrets(client, tmp_path):
    from aubergeRP.config import get_config
    data_dir = Path(get_config().app.data_dir)
    from aubergeRP.database import init_db
    init_db(data_dir)
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="X", description="X")
    )
    bot = client.post("/api/telegram/bots/", json={
        "name": "Bot",
        "token": "top_secret_token",
        "character_id": char.id,
        "enabled": False,
    }).json()

    resp = client.get(f"/api/telegram/bots/{bot['id']}/status")
    assert resp.status_code == 200
    assert "top_secret_token" not in resp.text
