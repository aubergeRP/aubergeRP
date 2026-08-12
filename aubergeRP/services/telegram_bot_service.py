"""TelegramBotService — CRUD for Telegram bot configurations.

Token security contract
-----------------------
- Tokens are stored in the DB as plaintext (same model as connector API keys
  stored in data/connectors/*.json), protected by OS-level directory perms.
- The API layer (router) *must never* return the token in any response.
  This service deliberately omits the token from all public output models.
- Editing a bot without supplying a new token preserves the existing token.
- Tokens are never logged at any level.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlmodel import Session, select

from ..db_models import ChannelSessionRow, TelegramBotRow


class TelegramBotNotFoundError(KeyError):
    pass


class TelegramBotConflictError(ValueError):
    pass


class TelegramBotInvalidError(ValueError):
    """Invalid bot configuration (e.g. webhook mode without a public URL)."""


# ── Public Pydantic models (token deliberately absent) ───────────────────────


class TelegramBotSummary(BaseModel):
    """Safe public view of a bot — no token, no raw webhook_secret."""

    id: str
    name: str
    character_id: str
    enabled: bool
    dialogue_only: bool
    telegram_bot_id: str
    telegram_username: str
    last_tested_at: datetime | None
    last_error: str
    update_mode: str
    webhook_url: str
    # True when a webhook secret is configured; the raw value is never returned.
    webhook_secret_set: bool
    webhook_last_error: str
    created_at: datetime
    updated_at: datetime


class TelegramBotCreate(BaseModel):
    name: str
    token: str
    character_id: str
    enabled: bool = False
    dialogue_only: bool = False
    update_mode: Literal["polling", "webhook"] = "polling"
    webhook_url: str = ""
    webhook_secret: str = ""


class TelegramBotUpdate(BaseModel):
    name: str | None = None
    # If None, keep the existing token.
    token: str | None = None
    character_id: str | None = None
    enabled: bool | None = None
    dialogue_only: bool | None = None
    update_mode: Literal["polling", "webhook"] | None = None
    webhook_url: str | None = None
    # If None, keep the existing secret. Pass "" to clear it.
    webhook_secret: str | None = None


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _row_to_summary(row: TelegramBotRow) -> TelegramBotSummary:
    return TelegramBotSummary(
        id=row.id,
        name=row.name,
        character_id=row.character_id,
        enabled=row.enabled,
        dialogue_only=row.dialogue_only,
        telegram_bot_id=row.telegram_bot_id,
        telegram_username=row.telegram_username,
        last_tested_at=_ensure_utc(row.last_tested_at) if row.last_tested_at else None,
        last_error=row.last_error,
        update_mode=row.update_mode,
        webhook_url=row.webhook_url,
        webhook_secret_set=bool(row.webhook_secret),
        webhook_last_error=row.webhook_last_error,
        created_at=_ensure_utc(row.created_at),
        updated_at=_ensure_utc(row.updated_at),
    )


class TelegramBotService:
    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def list_bots(self) -> list[TelegramBotSummary]:
        with self._get_session() as session:
            rows = session.exec(select(TelegramBotRow)).all()
            return [_row_to_summary(r) for r in rows]

    def get_bot(self, bot_id: str) -> TelegramBotSummary:
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            return _row_to_summary(row)

    def get_bot_token(self, bot_id: str) -> str:
        """Return the raw token — for internal use by the runtime manager only."""
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            return row.token

    @staticmethod
    def _validate_webhook(update_mode: str, webhook_url: str) -> None:
        """Webhook mode is unusable without a public HTTPS base URL."""
        if update_mode != "webhook":
            return
        url = webhook_url.strip()
        if not url:
            raise TelegramBotInvalidError(
                "A public webhook URL is required when update mode is 'webhook'."
            )
        if not url.startswith("https://"):
            raise TelegramBotInvalidError(
                "The webhook URL must start with https:// — Telegram refuses plain HTTP."
            )

    def create_bot(self, data: TelegramBotCreate) -> TelegramBotSummary:
        self._validate_webhook(data.update_mode, data.webhook_url)
        now = datetime.now(UTC)
        row = TelegramBotRow(
            id=str(uuid.uuid4()),
            name=data.name,
            token=data.token,
            character_id=data.character_id,
            enabled=data.enabled,
            dialogue_only=data.dialogue_only,
            update_mode=data.update_mode,
            webhook_url=data.webhook_url,
            webhook_secret=data.webhook_secret,
            created_at=now,
            updated_at=now,
        )
        with self._get_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_summary(row)

    def update_bot(self, bot_id: str, data: TelegramBotUpdate) -> TelegramBotSummary:
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            if data.name is not None:
                row.name = data.name
            if data.token is not None and data.token != "":
                # Only update token if a non-empty value was provided.
                row.token = data.token
            if data.character_id is not None:
                row.character_id = data.character_id
            if data.enabled is not None:
                row.enabled = data.enabled
            if data.dialogue_only is not None:
                row.dialogue_only = data.dialogue_only
            if data.update_mode is not None:
                row.update_mode = data.update_mode
            if data.webhook_url is not None:
                row.webhook_url = data.webhook_url
            self._validate_webhook(row.update_mode, row.webhook_url)
            if data.webhook_secret is not None:
                # Allow explicit "" to clear the secret; otherwise keep existing.
                row.webhook_secret = data.webhook_secret
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_summary(row)

    def set_enabled(self, bot_id: str, enabled: bool) -> TelegramBotSummary:
        return self.update_bot(bot_id, TelegramBotUpdate(enabled=enabled))

    def delete_bot(self, bot_id: str) -> None:
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            # Also delete all channel sessions for this bot.
            sessions = session.exec(
                select(ChannelSessionRow).where(
                    ChannelSessionRow.channel == "telegram",
                    ChannelSessionRow.channel_instance_id == bot_id,
                )
            ).all()
            for cs in sessions:
                session.delete(cs)
            session.delete(row)
            session.commit()

    def record_test_result(
        self,
        bot_id: str,
        telegram_bot_id: str,
        telegram_username: str,
        error: str = "",
    ) -> TelegramBotSummary:
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            now = datetime.now(UTC)
            row.last_tested_at = now
            row.last_error = error
            if not error:
                row.telegram_bot_id = telegram_bot_id
                row.telegram_username = telegram_username
            row.updated_at = now
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_summary(row)

    def record_webhook_error(self, bot_id: str, error: str) -> TelegramBotSummary:
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            row.webhook_last_error = error
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_summary(row)

    def get_bot_webhook_secret(self, bot_id: str) -> str:
        """Return the raw webhook secret — for internal use by the router only."""
        with self._get_session() as session:
            row = session.get(TelegramBotRow, bot_id)
            if row is None:
                raise TelegramBotNotFoundError(bot_id)
            return row.webhook_secret

    def generate_webhook_secret(self, bot_id: str) -> TelegramBotSummary:
        """Generate and persist a new cryptographic webhook secret for the bot."""
        new_secret = secrets.token_urlsafe(32)
        return self.update_bot(bot_id, TelegramBotUpdate(webhook_secret=new_secret))

    def list_enabled_bots_with_tokens(self) -> list[tuple[TelegramBotSummary, str]]:
        """Return [(summary, token)] for all enabled bots.  Internal use only."""
        with self._get_session() as session:
            rows = session.exec(
                select(TelegramBotRow).where(TelegramBotRow.enabled == True)  # noqa: E712
            ).all()
            return [(_row_to_summary(r), r.token) for r in rows]

    def character_is_referenced(self, character_id: str) -> bool:
        """Return True if any Telegram bot references this character."""
        with self._get_session() as session:
            row = session.exec(
                select(TelegramBotRow).where(TelegramBotRow.character_id == character_id)
            ).first()
            return row is not None
