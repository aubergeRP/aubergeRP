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


class SchemaMigration(SQLModel, table=True):
    """Tracks which migrations have been applied."""

    __tablename__ = "schema_migrations"

    version: int = Field(primary_key=True)
    description: str = ""
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
