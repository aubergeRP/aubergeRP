"""SQLModel table definitions for aubergeRP.

These are the canonical on-disk representations.  The Pydantic *models*
(CharacterCard, Conversation, …) remain unchanged and are still used for
business logic — service classes convert between the two.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Field, SQLModel


class CharacterRow(SQLModel, table=True):
    """One row per character card."""

    __tablename__ = "characters"

    id: str = Field(primary_key=True)
    has_avatar: bool = False
    spec: str = "chara_card_v2"
    spec_version: str = "2.0"
    # CharacterData serialised as JSON
    data_json: str = Field(default="{}")
    created_at: datetime
    updated_at: datetime

    def get_data(self) -> dict[str, Any]:
        return json.loads(self.data_json)  # type: ignore[no-any-return]


class ConversationRow(SQLModel, table=True):
    """One row per conversation (without its messages)."""

    __tablename__ = "conversations"

    id: str = Field(primary_key=True)
    character_id: str
    character_name: str
    title: str
    owner: str = ""
    created_at: datetime
    updated_at: datetime


class MessageRow(SQLModel, table=True):
    """One row per chat message."""

    __tablename__ = "messages"

    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True)
    role: str
    content: str
    # list[str] serialised as JSON
    images_json: str = Field(default="[]")
    timestamp: datetime

    def get_images(self) -> list[str]:
        return json.loads(self.images_json)  # type: ignore[no-any-return]

    @staticmethod
    def images_to_json(images: list[str]) -> str:
        return json.dumps(images)


class MediaRow(SQLModel, table=True):
    """One row per generated media entry shown in the admin media library."""

    __tablename__ = "media_library"

    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True)
    message_id: str = Field(index=True)
    owner: str = Field(default="", index=True)
    media_type: str = Field(default="image")
    media_url: str
    prompt: str = ""
    generated_via_connector: bool = True
    created_at: datetime


class LLMCallStatRow(SQLModel, table=True):
    """One row per remote text-LLM call used by chat generation."""

    __tablename__ = "llm_call_stats"

    id: str = Field(primary_key=True)
    conversation_id: str = Field(index=True)
    connector_id: str = ""
    connector_name: str = ""
    connector_backend: str = ""
    request_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0
    response_time_ms: int = 0
    success: bool = True
    error_detail: str = ""
    created_at: datetime


class TelegramBotRow(SQLModel, table=True):
    """One row per configured Telegram bot."""

    __tablename__ = "telegram_bots"

    id: str = Field(primary_key=True)
    name: str
    # Token is stored as-is (secret file storage handled by file_storage).
    # The API layer never returns this field in plaintext.
    token: str
    character_id: str
    enabled: bool = False
    dialogue_only: bool = False
    # Populated after a successful test connection
    telegram_bot_id: str = ""
    telegram_username: str = ""
    last_tested_at: datetime | None = None
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class ChannelSessionRow(SQLModel, table=True):
    """Transport-neutral mapping: external user/chat → AubergeRP conversation."""

    __tablename__ = "channel_sessions"

    id: str = Field(primary_key=True)
    channel: str = Field(index=True)           # e.g. "telegram"
    channel_instance_id: str = Field(index=True)  # e.g. TelegramBotRow.id
    external_user_id: str = Field(index=True)
    external_chat_id: str
    conversation_id: str
    created_at: datetime
    updated_at: datetime


class UserTimezoneRow(SQLModel, table=True):
    """Transport-neutral per-user timezone storage.

    Keyed by (channel, channel_instance_id, external_user_id), which mirrors
    the ChannelSessionRow identity without coupling to a conversation.

    Web sessions use channel="web", channel_instance_id="web".
    Telegram sessions use channel="telegram", channel_instance_id=<bot_id>.
    """

    __tablename__ = "user_timezones"

    id: str = Field(primary_key=True)
    channel: str = Field(index=True)
    channel_instance_id: str = Field(index=True)
    external_user_id: str = Field(index=True)
    # IANA timezone identifier, e.g. "Europe/Paris"
    timezone: str
    updated_at: datetime


class ScheduleInstanceRow(SQLModel, table=True):
    """Runtime execution state for one character schedule × one conversation.

    A single ``ScheduleDefinition`` in a character card may produce many
    independent ``ScheduleInstanceRow`` rows — one per (character, schedule,
    conversation/user/channel) combination.

    The card definition is the source of truth for *what* to do and the default
    enabled state.  This row tracks *when* it was last run, when it is next due,
    and prevents duplicate generation through the ``generation_started_at`` lock.
    """

    __tablename__ = "schedule_instances"

    id: str = Field(primary_key=True)
    # Matches ScheduleDefinition.id inside the card extensions
    schedule_def_id: str = Field(index=True)
    character_id: str = Field(index=True)
    conversation_id: str = Field(index=True)
    channel: str        # "telegram" | "web"
    channel_instance_id: str
    external_user_id: str
    external_chat_id: str = ""  # Telegram chat_id, or empty for web
    # Per-instance runtime override (the card definition is the default)
    enabled: bool = True
    # IANA timezone, copied from user_timezones at instance creation / update
    timezone: str = "UTC"
    last_run_at: datetime | None = None
    # Pre-calculated next trigger time (UTC).  Recalculated after each run and
    # whenever the timezone or schedule definition changes.
    next_run_at: datetime | None = None
    # Idempotency lock: set to "now" when generation begins, cleared on success.
    # If the server restarts while non-None, generation did not complete and the
    # row is eligible for a fresh attempt.
    generation_started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SchemaMigration(SQLModel, table=True):
    """Tracks which migrations have been applied."""

    __tablename__ = "schema_migrations"

    version: int = Field(primary_key=True)
    description: str = ""
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
