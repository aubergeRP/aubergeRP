"""Tests for Telegram bot admin CRUD, token security, and runtime behaviour."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
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
from aubergeRP.services.telegram_runtime_manager import (
    GENERATION_FAILURE_MESSAGE,
    TelegramRuntimeManager,
    chat_action,
    split_message,
)

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


def test_runtime_reset_returns_character_first_message(tmp_path):
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test", first_mes="Hello, I am {{char}}.")
    )

    mgr = TelegramRuntimeManager(data_dir=data_dir)
    conv_id, greeting = mgr._reset_session("bot-1", "user-1", "1", char.id)
    assert conv_id
    assert greeting == "Hello, I am Alice."


def test_runtime_reset_without_first_message_returns_empty_greeting(tmp_path):
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Bob", description="Test")
    )

    mgr = TelegramRuntimeManager(data_dir=data_dir)
    _conv_id, greeting = mgr._reset_session("bot-1", "user-1", "1", char.id)
    assert greeting == ""


def test_runtime_reset_moves_schedule_instances_to_new_conversation(tmp_path):
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    from aubergeRP.services.schedule_instance_service import ScheduleInstanceService
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(
            name="Alice",
            description="Test",
            extensions={"aubergerp": {"schedules": [{
                "id": "morning",
                "enabled": True,
                "type": "daily_at",
                "time": "08:00",
                "instruction": "Say good morning",
            }]}},
        )
    )

    mgr = TelegramRuntimeManager(data_dir=data_dir)
    conv1, _ = mgr._get_or_create_session("bot-1", "user-1", "1", char.id)
    sched_svc = ScheduleInstanceService(data_dir)
    assert len(sched_svc.list_for_conversation(conv1)) == 1

    conv2, _ = mgr._reset_session("bot-1", "user-1", "1", char.id)
    assert conv2 != conv1
    assert sched_svc.list_for_conversation(conv1) == []
    assert len(sched_svc.list_for_conversation(conv2)) == 1


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


# ── Webhook mode ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_mode_start_uses_run_bot_webhook(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)

    started: list[str] = []

    async def fake_run_webhook(bot_id, token, character_id, dialogue_only=False, webhook_url=""):
        started.append(bot_id)
        await asyncio.sleep(0.1)

    with patch.object(mgr, "_run_bot_webhook", side_effect=fake_run_webhook):
        await mgr.start_bot("bot-wh", "tok", "char1", update_mode="webhook", webhook_url="https://example.com")
        await asyncio.sleep(0.01)
        assert mgr.is_running("bot-wh")
        await mgr.stop_all()
    assert "bot-wh" in started


@pytest.mark.asyncio
async def test_webhook_mode_stop_calls_delete_webhook(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)

    bot_mock = AsyncMock()
    bot_mock.delete_webhook = AsyncMock()
    bot_mock.session = AsyncMock()
    mgr._bots["bot-wh"] = bot_mock
    mgr._modes["bot-wh"] = "webhook"

    async def fake_run_webhook(bot_id, token, character_id, dialogue_only=False, webhook_url=""):
        await asyncio.sleep(10)

    with patch.object(mgr, "_run_bot_webhook", side_effect=fake_run_webhook):
        await mgr.start_bot("bot-wh", "tok", "char1", update_mode="webhook", webhook_url="https://example.com")
        await asyncio.sleep(0.01)
        await mgr.stop_bot("bot-wh")
    bot_mock.delete_webhook.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_update_feeds_dispatcher(tmp_path):
    import sys

    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)

    bot_mock = AsyncMock()
    dp_mock = AsyncMock()
    dp_mock.feed_update = AsyncMock()
    mgr._bots["bot-wh"] = bot_mock
    mgr._dispatchers["bot-wh"] = dp_mock

    update_instance = MagicMock()
    update_cls = MagicMock()
    update_cls.model_validate.return_value = update_instance

    aiogram_mock = MagicMock()
    aiogram_types_mock = MagicMock()
    aiogram_types_mock.Update = update_cls

    with patch.dict(sys.modules, {"aiogram": aiogram_mock, "aiogram.types": aiogram_types_mock}):
        await mgr.dispatch_update("bot-wh", {"update_id": 1})

    update_cls.model_validate.assert_called_once_with({"update_id": 1})
    dp_mock.feed_update.assert_called_once_with(bot_mock, update_instance)


@pytest.mark.asyncio
async def test_dispatch_update_no_op_when_no_dispatcher(tmp_path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)

    mgr = TelegramRuntimeManager(data_dir=data_dir)
    # No bot/dispatcher registered — should not raise.
    await mgr.dispatch_update("missing-bot", {"update_id": 99})


# ── Webhook receiving endpoint ────────────────────────────────────────────────


def _make_webhook_bot(client, *, enabled=True, secret="s3cret"):
    """Create a bot configured for webhook mode; return its summary dict."""
    from aubergeRP.config import get_config
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = Path(get_config().app.data_dir)
    init_db(data_dir)
    char = CharacterService(data_dir=data_dir).create_character(
        CharacterData(name="Alice", description="Test")
    )
    return client.post("/api/telegram/bots/", json={
        "name": "WH Bot",
        "token": "tok_wh",
        "character_id": char.id,
        "enabled": enabled,
        "update_mode": "webhook",
        "webhook_url": "https://example.test",
        "webhook_secret": secret,
    }).json()


def test_webhook_endpoint_dispatches_update(client):
    bot = _make_webhook_bot(client)
    mgr = MagicMock()
    mgr.dispatch_update = AsyncMock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.post(
            f"/api/telegram/webhook/{bot['id']}",
            json={"update_id": 42},
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mgr.dispatch_update.assert_awaited_once_with(bot["id"], {"update_id": 42})


def test_webhook_endpoint_rejects_bad_secret(client):
    bot = _make_webhook_bot(client)
    mgr = MagicMock()
    mgr.dispatch_update = AsyncMock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.post(
            f"/api/telegram/webhook/{bot['id']}",
            json={"update_id": 42},
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
    assert resp.status_code == 403
    mgr.dispatch_update.assert_not_awaited()


def test_webhook_endpoint_rejects_missing_secret_header(client):
    bot = _make_webhook_bot(client)
    resp = client.post(f"/api/telegram/webhook/{bot['id']}", json={"update_id": 1})
    assert resp.status_code == 403


def test_webhook_endpoint_unknown_bot(client):
    resp = client.post("/api/telegram/webhook/nope", json={"update_id": 1})
    assert resp.status_code == 404


def test_webhook_endpoint_disabled_bot(client):
    bot = _make_webhook_bot(client, enabled=False)
    resp = client.post(
        f"/api/telegram/webhook/{bot['id']}",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"},
    )
    assert resp.status_code == 409


def test_webhook_endpoint_no_secret_configured_accepts(client):
    bot = _make_webhook_bot(client, secret="")
    mgr = MagicMock()
    mgr.dispatch_update = AsyncMock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.post(f"/api/telegram/webhook/{bot['id']}", json={"update_id": 7})
    assert resp.status_code == 200
    mgr.dispatch_update.assert_awaited_once()


def test_webhook_endpoint_rejects_invalid_payload(client):
    bot = _make_webhook_bot(client)
    resp = client.post(
        f"/api/telegram/webhook/{bot['id']}",
        content=b"not json",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": "s3cret",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 400


# ── Bot profile sync ─────────────────────────────────────────────────────────


def _profile_bot_mock() -> MagicMock:
    """A Bot mock whose getters report an empty profile, then what was set.

    The setters update the state the getters report, so the read-back
    verification performed by the sync sees the value it just pushed.
    """
    bot = MagicMock()
    state = {"name": "", "description": "", "short_description": ""}
    bot.profile_state = state

    def _getter(field: str) -> AsyncMock:
        return AsyncMock(side_effect=lambda: SimpleNamespace(**{field: state[field]}))

    def _setter(field: str) -> AsyncMock:
        return AsyncMock(side_effect=lambda value: state.__setitem__(field, value))

    bot.get_my_name = _getter("name")
    bot.get_my_description = _getter("description")
    bot.get_my_short_description = _getter("short_description")
    bot.set_my_name = _setter("name")
    bot.set_my_description = _setter("description")
    bot.set_my_short_description = _setter("short_description")
    bot.set_my_profile_photo = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_profile_sync_pushes_name_and_descriptions(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    await mgr._sync_bot_profile("bot-1", bot, char_id)

    bot.set_my_name.assert_awaited_once_with("Alice")
    bot.set_my_description.assert_awaited_once_with("Test character")
    bot.set_my_short_description.assert_awaited_once_with("Test character")


@pytest.mark.asyncio
async def test_profile_sync_skips_unchanged_fields(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()
    bot.get_my_name = AsyncMock(return_value=SimpleNamespace(name="Alice"))
    bot.get_my_description = AsyncMock(return_value=SimpleNamespace(description="Test character"))

    await mgr._sync_bot_profile("bot-1", bot, char_id)

    bot.set_my_name.assert_not_awaited()
    bot.set_my_description.assert_not_awaited()
    bot.set_my_short_description.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_sync_survives_api_errors(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()
    bot.set_my_name = AsyncMock(side_effect=RuntimeError("flood wait"))

    # Must not raise — a failed sync never prevents the bot from starting.
    await mgr._sync_bot_profile("bot-1", bot, char_id)

    bot.set_my_description.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_sync_unknown_character_is_noop(tmp_path):
    _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    await mgr._sync_bot_profile("bot-1", bot, "does-not-exist")

    bot.set_my_name.assert_not_awaited()


def _write_avatar(tmp_path, char_id: str, color: str = "red") -> None:
    import io

    from PIL import Image

    from aubergeRP.services.character_service import CharacterService
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), color).save(buf, format="PNG")
    CharacterService(data_dir=tmp_path / "data").save_avatar(char_id, buf.getvalue())


@pytest.mark.asyncio
async def test_profile_photo_uploaded_once_per_avatar(tmp_path):
    import sys

    char_id = _make_character(tmp_path)
    _write_avatar(tmp_path, char_id)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    types_mock = MagicMock()
    with patch.dict(sys.modules, {"aiogram.types": types_mock}):
        await mgr._sync_bot_profile("bot-1", bot, char_id)
        assert bot.set_my_profile_photo.await_count == 1

        # Same avatar → no re-upload.
        await mgr._sync_bot_profile("bot-1", bot, char_id)
        assert bot.set_my_profile_photo.await_count == 1

        # Changed avatar → uploaded again.
        _write_avatar(tmp_path, char_id, color="blue")
        await mgr._sync_bot_profile("bot-1", bot, char_id)
        assert bot.set_my_profile_photo.await_count == 2


@pytest.mark.asyncio
async def test_profile_sync_retries_when_field_did_not_stick(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()
    # A setter that silently does nothing (e.g. swallowed flood wait).
    bot.set_my_name = AsyncMock()

    await mgr._sync_bot_profile("bot-1", bot, char_id)

    assert bot.set_my_name.await_count == 2


def _fake_text_connector(answer: str) -> MagicMock:
    conn = MagicMock()

    async def _stream(*_args, **_kwargs):
        yield answer

    conn.stream_chat_completion = _stream
    return conn


@pytest.mark.asyncio
async def test_profile_bio_generated_and_cached(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    manager = MagicMock()
    manager.get_text_connector.return_value = _fake_text_connector(
        '{"description": "I am Alice.", "short_description": "Alice, innkeeper."}'
    )
    manager.get_active_image_connector.return_value = None
    with patch("aubergeRP.connectors.manager.ConnectorManager", return_value=manager):
        await mgr._sync_bot_profile("bot-1", bot, char_id)
        assert bot.profile_state["description"] == "I am Alice."
        assert bot.profile_state["short_description"] == "Alice, innkeeper."

        # Second sync reuses the cache — no further LLM call.
        manager.get_text_connector.reset_mock()
        await mgr._sync_bot_profile("bot-1", bot, char_id)
        manager.get_text_connector.assert_not_called()


@pytest.mark.asyncio
async def test_profile_bio_falls_back_to_truncated_card(tmp_path):
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService

    _make_character(tmp_path)
    svc = CharacterService(data_dir=tmp_path / "data")
    long_desc = "word " * 400
    char_id = svc.create_character(CharacterData(name="Bob", description=long_desc)).id

    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    manager = MagicMock()
    manager.get_text_connector.return_value = None
    manager.get_active_image_connector.return_value = None
    with patch("aubergeRP.connectors.manager.ConnectorManager", return_value=manager):
        await mgr._sync_bot_profile("bot-1", bot, char_id)

    assert 0 < len(bot.profile_state["description"]) <= 512
    assert 0 < len(bot.profile_state["short_description"]) <= 120


@pytest.mark.asyncio
async def test_missing_avatar_is_generated_from_character(tmp_path):
    import io
    import sys

    from PIL import Image

    from aubergeRP.services.character_service import CharacterService

    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), "green").save(buf, format="PNG")

    img_connector = MagicMock()

    async def _gen(prompt, negative_prompt=""):
        _gen.prompt = prompt
        yield {"type": "complete", "bytes": buf.getvalue()}

    img_connector.generate_image_with_progress = _gen

    manager = MagicMock()
    manager.get_text_connector.return_value = _fake_text_connector(
        "portrait of Alice, close-up, soft light"
    )
    manager.get_active_image_connector.return_value = img_connector

    types_mock = MagicMock()
    with patch("aubergeRP.connectors.manager.ConnectorManager", return_value=manager), \
         patch.dict(sys.modules, {"aiogram.types": types_mock}):
        await mgr._sync_bot_profile("bot-1", bot, char_id)

    assert CharacterService(data_dir=tmp_path / "data").get_avatar_path(char_id) is not None
    bot.set_my_profile_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_photo_skipped_without_avatar(tmp_path):
    char_id = _make_character(tmp_path)
    mgr = TelegramRuntimeManager(data_dir=tmp_path / "data")
    bot = _profile_bot_mock()

    await mgr._sync_bot_profile("bot-1", bot, char_id)

    bot.set_my_profile_photo.assert_not_awaited()


# ── Webhook configuration validation ─────────────────────────────────────────


def test_create_webhook_bot_requires_url(client, tmp_path):
    char_id = _make_character(tmp_path)
    resp = client.post("/api/telegram/bots/", json={
        "name": "WH", "token": "123:AAA", "character_id": char_id,
        "update_mode": "webhook",
    })
    assert resp.status_code == 400
    assert "webhook URL" in resp.json()["detail"]


def test_create_webhook_bot_rejects_plain_http(client, tmp_path):
    char_id = _make_character(tmp_path)
    resp = client.post("/api/telegram/bots/", json={
        "name": "WH", "token": "123:AAA", "character_id": char_id,
        "update_mode": "webhook", "webhook_url": "http://rp.example.com",
    })
    assert resp.status_code == 400
    assert "https://" in resp.json()["detail"]


def test_create_webhook_bot_with_https_url(client, tmp_path):
    char_id = _make_character(tmp_path)
    resp = client.post("/api/telegram/bots/", json={
        "name": "WH", "token": "123:AAA", "character_id": char_id,
        "update_mode": "webhook", "webhook_url": "https://rp.example.com",
        "webhook_secret": "s3cret",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["update_mode"] == "webhook"
    assert body["webhook_url"] == "https://rp.example.com"
    assert body["webhook_secret_set"] is True
    assert "webhook_secret" not in body


def test_switching_to_webhook_without_url_is_rejected(client, tmp_path):
    char_id = _make_character(tmp_path)
    created = client.post("/api/telegram/bots/", json={
        "name": "Poll", "token": "123:AAA", "character_id": char_id,
    }).json()
    resp = client.patch(f"/api/telegram/bots/{created['id']}", json={"update_mode": "webhook"})
    assert resp.status_code == 400
    # Configuration is left untouched.
    assert client.get(f"/api/telegram/bots/{created['id']}").json()["update_mode"] == "polling"


def test_update_restarts_running_bot(client, tmp_path):
    char_id = _make_character(tmp_path)
    created = client.post("/api/telegram/bots/", json={
        "name": "Poll", "token": "123:AAA", "character_id": char_id,
    }).json()

    mgr = MagicMock()
    mgr.is_running.return_value = True
    mgr.restart_bot = AsyncMock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.patch(f"/api/telegram/bots/{created['id']}", json={
            "update_mode": "webhook", "webhook_url": "https://rp.example.com",
        })
    assert resp.status_code == 200
    mgr.restart_bot.assert_awaited_once_with(created["id"])


def test_update_does_not_restart_stopped_bot(client, tmp_path):
    char_id = _make_character(tmp_path)
    created = client.post("/api/telegram/bots/", json={
        "name": "Poll", "token": "123:AAA", "character_id": char_id,
    }).json()

    mgr = MagicMock()
    mgr.is_running.return_value = False
    mgr.restart_bot = AsyncMock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.patch(f"/api/telegram/bots/{created['id']}", json={"name": "Renamed"})
    assert resp.status_code == 200
    mgr.restart_bot.assert_not_awaited()


# ── Runtime is started on create / update / test ──────────────────────────────


def _runtime_mock(running=False):
    mgr = MagicMock()
    mgr.is_running.return_value = running
    mgr.start_bot = AsyncMock()
    mgr.restart_bot = AsyncMock()
    return mgr


def test_create_enabled_bot_starts_runtime(client, tmp_path):
    """An enabled bot must run right away — otherwise its webhook is never set."""
    char_id = _make_character(tmp_path)
    mgr = _runtime_mock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.post("/api/telegram/bots/", json={
            "name": "WH", "token": "123:AAA", "character_id": char_id, "enabled": True,
            "update_mode": "webhook", "webhook_url": "https://rp.example.com",
        })
    assert resp.status_code == 201
    mgr.start_bot.assert_awaited_once()
    kwargs = mgr.start_bot.await_args.kwargs
    assert kwargs["update_mode"] == "webhook"
    assert kwargs["webhook_url"] == "https://rp.example.com"


def test_create_disabled_bot_does_not_start_runtime(client, tmp_path):
    char_id = _make_character(tmp_path)
    mgr = _runtime_mock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.post("/api/telegram/bots/", json={
            "name": "Poll", "token": "123:AAA", "character_id": char_id,
        })
    assert resp.status_code == 201
    mgr.start_bot.assert_not_awaited()


def test_update_starts_enabled_but_stopped_bot(client, tmp_path):
    char_id = _make_character(tmp_path)
    created = client.post("/api/telegram/bots/", json={
        "name": "Poll", "token": "123:AAA", "character_id": char_id,
    }).json()

    mgr = _runtime_mock()
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr):
        resp = client.patch(f"/api/telegram/bots/{created['id']}", json={"enabled": True})
    assert resp.status_code == 200
    mgr.restart_bot.assert_not_awaited()
    mgr.start_bot.assert_awaited_once()


def test_test_connection_starts_stopped_enabled_bot(client, tmp_path):
    """Back-compat: 'Test connection' launches a bot that was never started."""
    char_id = _make_character(tmp_path)
    with patch("aubergeRP.routers.telegram._get_manager", return_value=_runtime_mock()):
        created = client.post("/api/telegram/bots/", json={
            "name": "WH", "token": "123:AAA", "character_id": char_id, "enabled": True,
            "update_mode": "webhook", "webhook_url": "https://rp.example.com",
        }).json()

    me_mock = MagicMock()
    me_mock.id = 42
    me_mock.username = "wh_bot"
    bot_mock = AsyncMock()
    bot_mock.get_me = AsyncMock(return_value=me_mock)
    bot_mock.session = AsyncMock()

    mgr = _runtime_mock()
    bot_cls = MagicMock(return_value=bot_mock)
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr), \
         patch("aubergeRP.routers.telegram.Bot", bot_cls):
        resp = client.post(f"/api/telegram/bots/{created['id']}/test")
    assert resp.status_code == 200
    mgr.start_bot.assert_awaited_once()


def test_test_connection_does_not_start_on_failure(client, tmp_path):
    char_id = _make_character(tmp_path)
    with patch("aubergeRP.routers.telegram._get_manager", return_value=_runtime_mock()):
        created = client.post("/api/telegram/bots/", json={
            "name": "WH", "token": "123:AAA", "character_id": char_id, "enabled": True,
        }).json()

    mgr = _runtime_mock()
    bot_cls = MagicMock(side_effect=RuntimeError("unauthorized"))
    with patch("aubergeRP.routers.telegram._get_manager", return_value=mgr), \
         patch("aubergeRP.routers.telegram.Bot", bot_cls):
        resp = client.post(f"/api/telegram/bots/{created['id']}/test")
    assert resp.status_code == 200
    mgr.start_bot.assert_not_awaited()


# ── chat action ("typing") ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_action_sent_and_stopped():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock()

    async with chat_action(bot, "42"):
        await asyncio.sleep(0)  # let the refresh task run once

    bot.send_chat_action.assert_awaited_with(chat_id=42, action="typing")
    calls = bot.send_chat_action.await_count
    await asyncio.sleep(0)
    assert bot.send_chat_action.await_count == calls  # task cancelled


@pytest.mark.asyncio
async def test_chat_action_failure_is_ignored():
    bot = MagicMock()
    bot.send_chat_action = AsyncMock(side_effect=RuntimeError("boom"))

    async with chat_action(bot, "42", "upload_photo"):
        await asyncio.sleep(0)


# ── Generation failure notice ────────────────────────────────────────────────


def _on_message_handler(mgr, dp, bot):
    mgr._register_handlers(dp, "bot1", "char1", bot)
    return dp.message.handlers[-1].callback


@pytest.mark.asyncio
async def test_generation_failure_sends_laconic_reply(tmp_path):
    from aiogram import Dispatcher

    mgr = TelegramRuntimeManager(data_dir=str(tmp_path))
    handler = _on_message_handler(mgr, Dispatcher(), MagicMock())

    mgr._get_or_create_session = MagicMock(return_value=("conv1", True))
    mgr._generate = AsyncMock(side_effect=RuntimeError("llm down"))

    message = MagicMock()
    message.text = "hello"
    message.caption = None
    message.photo = None
    message.chat = SimpleNamespace(id=42, type="private")
    message.from_user = SimpleNamespace(id=7)
    message.answer = AsyncMock()

    with patch("aubergeRP.services.telegram_runtime_manager.chat_action"):
        await handler(message)

    message.answer.assert_awaited_once_with(GENERATION_FAILURE_MESSAGE)
