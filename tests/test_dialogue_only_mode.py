"""Tests for Step 3 — dialogue-only narration mode."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aubergeRP.models.character import CharacterCard, CharacterData
from aubergeRP.models.conversation import Conversation
from aubergeRP.services.chat_service import GenerationOptions, build_prompt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _char(**overrides) -> CharacterCard:
    base = dict(name="Alice", description="A friendly assistant.")
    base.update(overrides)
    data = CharacterData(**base)
    return CharacterCard(id="c1", has_avatar=False, created_at=_now(), updated_at=_now(), data=data)


def _conv(char: CharacterCard, messages: list | None = None) -> Conversation:
    return Conversation(
        id="conv-1",
        character_id=char.id,
        character_name=char.data.name,
        title=char.data.name,
        messages=messages or [],
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# GenerationOptions defaults
# ---------------------------------------------------------------------------


def test_generation_defaults_to_full_narration_mode() -> None:
    opts = GenerationOptions()
    assert opts.narration_mode == "full"


# ---------------------------------------------------------------------------
# build_prompt — dialogue-only mode
# ---------------------------------------------------------------------------


def test_dialogue_only_adds_channel_instruction() -> None:
    char = _char()
    conv = _conv(char)
    messages = build_prompt(conv, char, narration_mode="dialogue_only")
    system_contents = " ".join(m["content"] for m in messages if m["role"] == "system")
    assert (
        "instant-messaging" in system_contents
        or "messaging app" in system_contents.lower()
        or "narration" in system_contents
    )


def test_dialogue_only_instruction_requests_exclusion_of_narration_actions_thoughts() -> None:
    char = _char()
    conv = _conv(char)
    messages = build_prompt(conv, char, narration_mode="dialogue_only")
    system_contents = " ".join(m["content"] for m in messages if m["role"] == "system")
    # The instruction should explicitly mention what to exclude
    assert "narration" in system_contents.lower() or "actions" in system_contents.lower()


def test_dialogue_only_preserves_character_prompt() -> None:
    char = _char(system_prompt="You are Alice, a cheerful elf.")
    conv = _conv(char)
    messages = build_prompt(conv, char, narration_mode="dialogue_only")
    first_system = next(m for m in messages if m["role"] == "system")
    assert "You are Alice" in first_system["content"]


def test_dialogue_only_instruction_injected_after_history() -> None:
    """The dialogue-only instruction must appear as a late system message (after messages)."""
    from aubergeRP.models.conversation import Message
    char = _char()
    msg = Message(id="m1", role="user", content="Hello!", images=[], timestamp=_now())
    conv = _conv(char, messages=[msg])
    messages = build_prompt(conv, char, narration_mode="dialogue_only")
    # Last system message should be the dialogue-only instruction
    system_messages = [m for m in messages if m["role"] == "system"]
    last_system = system_messages[-1]
    assert "messaging" in last_system["content"].lower() or "narration" in last_system["content"].lower()


def test_full_mode_does_not_add_dialogue_only_instruction() -> None:
    char = _char()
    conv = _conv(char)
    messages_full = build_prompt(conv, char, narration_mode="full")
    system_contents = " ".join(m["content"] for m in messages_full if m["role"] == "system")
    # Should NOT contain the dialogue-only instruction's characteristic phrase
    assert "instant-messaging" not in system_contents


def test_web_generation_behavior_is_unchanged() -> None:
    """build_prompt with default narration_mode='full' is identical to old behavior."""
    char = _char()
    conv = _conv(char)
    messages_default = build_prompt(conv, char)
    messages_full = build_prompt(conv, char, narration_mode="full")
    assert messages_default == messages_full


# ---------------------------------------------------------------------------
# TelegramBotService — dialogue_only field
# ---------------------------------------------------------------------------


@pytest.fixture()
def tg_svc(tmp_path: Path):
    from aubergeRP.database import init_db
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    init_db(data_dir)
    from aubergeRP.services.telegram_bot_service import TelegramBotService
    return TelegramBotService(data_dir=data_dir)


def _seed_char(tmp_path: Path) -> str:
    from aubergeRP.database import init_db
    from aubergeRP.models.character import CharacterData
    from aubergeRP.services.character_service import CharacterService
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    init_db(data_dir)
    svc = CharacterService(data_dir=data_dir)
    card = svc.create_character(CharacterData(name="Alice", description="Test character"))
    return card.id


def test_telegram_bot_can_enable_dialogue_only(tg_svc, tmp_path: Path) -> None:
    from aubergeRP.services.telegram_bot_service import TelegramBotCreate
    char_id = _seed_char(tmp_path)
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Alice Bot",
        token="tok",
        character_id=char_id,
        dialogue_only=True,
    ))
    assert bot.dialogue_only is True


def test_telegram_bot_can_disable_dialogue_only(tg_svc, tmp_path: Path) -> None:
    from aubergeRP.services.telegram_bot_service import TelegramBotCreate
    char_id = _seed_char(tmp_path)
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Alice Bot",
        token="tok",
        character_id=char_id,
        dialogue_only=False,
    ))
    assert bot.dialogue_only is False


def test_dialogue_only_default_is_false(tg_svc, tmp_path: Path) -> None:
    from aubergeRP.services.telegram_bot_service import TelegramBotCreate
    char_id = _seed_char(tmp_path)
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Alice Bot",
        token="tok",
        character_id=char_id,
    ))
    assert bot.dialogue_only is False


def test_dialogue_only_setting_persists(tg_svc, tmp_path: Path) -> None:
    from aubergeRP.services.telegram_bot_service import TelegramBotCreate, TelegramBotUpdate
    char_id = _seed_char(tmp_path)
    bot = tg_svc.create_bot(TelegramBotCreate(
        name="Alice Bot",
        token="tok",
        character_id=char_id,
        dialogue_only=False,
    ))
    # Enable it
    updated = tg_svc.update_bot(bot.id, TelegramBotUpdate(dialogue_only=True))
    assert updated.dialogue_only is True
    # Reload and verify
    reloaded = tg_svc.get_bot(bot.id)
    assert reloaded.dialogue_only is True


# ---------------------------------------------------------------------------
# TelegramRuntimeManager — passes correct GenerationOptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_passes_correct_generation_option_dialogue_only() -> None:
    # Test that GenerationOptions correctly represents both modes.
    from aubergeRP.services.chat_service import GenerationOptions

    opts_enabled = GenerationOptions(narration_mode="dialogue_only")
    opts_disabled = GenerationOptions(narration_mode="full")
    assert opts_enabled.narration_mode == "dialogue_only"
    assert opts_disabled.narration_mode == "full"


@pytest.mark.asyncio
async def test_telegram_passes_dialogue_only_to_generation(tmp_path: Path) -> None:
    """_generate(dialogue_only=True) must build GenerationOptions with narration_mode='dialogue_only'."""
    from aubergeRP.services.telegram_runtime_manager import TelegramRuntimeManager
    mgr = TelegramRuntimeManager(data_dir=tmp_path)

    captured: list[GenerationOptions] = []

    async def mock_generate_reply(**kwargs):
        captured.append(kwargs.get("options"))
        result = MagicMock()
        result.text = "Hello!"
        return result

    mock_svc_instance = MagicMock()
    mock_svc_instance.generate_reply = mock_generate_reply

    mock_cfg = MagicMock()
    mock_cfg.app.data_dir = str(tmp_path)
    mock_cfg.chat.context_window = 4096
    mock_cfg.chat.summarization_threshold = 0.75
    mock_cfg.chat.ooc_protection = True

    with (
        patch("aubergeRP.config.get_config", return_value=mock_cfg),
        patch("aubergeRP.connectors.manager.ConnectorManager"),
        patch("aubergeRP.services.character_service.CharacterService"),
        patch("aubergeRP.services.conversation_service.ConversationService"),
        patch("aubergeRP.services.statistics_service.StatisticsService"),
        patch("aubergeRP.services.media_service.MediaService"),
        patch("aubergeRP.services.chat_service.ChatService", return_value=mock_svc_instance),
    ):
        await mgr._generate("conv-1", "hi", dialogue_only=True)

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].narration_mode == "dialogue_only"


@pytest.mark.asyncio
async def test_telegram_passes_full_mode_when_dialogue_only_false(tmp_path: Path) -> None:
    """_generate(dialogue_only=False) must build GenerationOptions with narration_mode='full'."""
    from aubergeRP.services.telegram_runtime_manager import TelegramRuntimeManager
    mgr = TelegramRuntimeManager(data_dir=tmp_path)

    captured: list[GenerationOptions] = []

    async def mock_generate_reply(**kwargs):
        captured.append(kwargs.get("options"))
        result = MagicMock()
        result.text = "Hello!"
        return result

    mock_svc_instance = MagicMock()
    mock_svc_instance.generate_reply = mock_generate_reply

    mock_cfg = MagicMock()
    mock_cfg.app.data_dir = str(tmp_path)
    mock_cfg.chat.context_window = 4096
    mock_cfg.chat.summarization_threshold = 0.75
    mock_cfg.chat.ooc_protection = True

    with (
        patch("aubergeRP.config.get_config", return_value=mock_cfg),
        patch("aubergeRP.connectors.manager.ConnectorManager"),
        patch("aubergeRP.services.character_service.CharacterService"),
        patch("aubergeRP.services.conversation_service.ConversationService"),
        patch("aubergeRP.services.statistics_service.StatisticsService"),
        patch("aubergeRP.services.media_service.MediaService"),
        patch("aubergeRP.services.chat_service.ChatService", return_value=mock_svc_instance),
    ):
        await mgr._generate("conv-1", "hi", dialogue_only=False)

    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0].narration_mode == "full"


# ---------------------------------------------------------------------------
# Migration — existing bots remain valid after migration 005
# ---------------------------------------------------------------------------


def test_migration_existing_bots_remain_valid(tmp_path: Path) -> None:
    """After migration 005, bots created before migration have dialogue_only=False."""
    from sqlalchemy import text
    from sqlmodel import Session, create_engine

    db_path = tmp_path / "auberge.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Create the table as it existed before migration 005
    with Session(engine) as session:
        session.execute(text("""
            CREATE TABLE telegram_bots (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                token TEXT NOT NULL DEFAULT '',
                character_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                telegram_bot_id TEXT NOT NULL DEFAULT '',
                telegram_username TEXT NOT NULL DEFAULT '',
                last_tested_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """))
        session.execute(text("""
            INSERT INTO telegram_bots
                (id, name, token, character_id, enabled, telegram_bot_id, telegram_username,
                 last_error, created_at, updated_at)
            VALUES
                ('bot-1', 'Alice Bot', 'tok', 'char-1', 0, '', '', '', '2024-01-01', '2024-01-01')
        """))
        session.commit()

    # Apply migration 005
    from aubergeRP.migrations.m005_add_dialogue_only import migrate
    with Session(engine) as session:
        migrate(session)
        session.commit()

    # Verify the bot still has dialogue_only=0 (False) as the default
    with Session(engine) as session:
        row = session.execute(
            text("SELECT dialogue_only FROM telegram_bots WHERE id='bot-1'")
        ).fetchone()
    assert row is not None
    assert row[0] == 0
