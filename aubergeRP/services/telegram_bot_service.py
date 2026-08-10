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

import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlmodel import Session, select

from ..db_models import ChannelSessionRow, TelegramBotRow


class TelegramBotNotFoundError(KeyError):
    pass


class TelegramBotConflictError(ValueError):
    pass


# ── Public Pydantic models (token deliberately absent) ───────────────────────


class TelegramBotSummary(BaseModel):
    """Safe public view of a bot — no token."""

    id: str
    name: str
    character_id: str
    enabled: bool
    telegram_bot_id: str
    telegram_username: str
    last_tested_at: datetime | None
    last_error: str
    created_at: datetime
    updated_at: datetime


class TelegramBotCreate(BaseModel):
    name: str
    token: str
    character_id: str
    enabled: bool = False


class TelegramBotUpdate(BaseModel):
    name: str | None = None
    # If None, keep the existing token.
    token: str | None = None
    character_id: str | None = None
    enabled: bool | None = None


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
        telegram_bot_id=row.telegram_bot_id,
        telegram_username=row.telegram_username,
        last_tested_at=_ensure_utc(row.last_tested_at) if row.last_tested_at else None,
        last_error=row.last_error,
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

    def create_bot(self, data: TelegramBotCreate) -> TelegramBotSummary:
        now = datetime.now(UTC)
        row = TelegramBotRow(
            id=str(uuid.uuid4()),
            name=data.name,
            token=data.token,
            character_id=data.character_id,
            enabled=data.enabled,
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
