"""Migration 004 — add telegram_bots and channel_sessions tables."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS telegram_bots (
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
            """
        )
    )

    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS channel_sessions (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                channel_instance_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    )

    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_sessions_channel "
            "ON channel_sessions (channel)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_sessions_channel_instance_id "
            "ON channel_sessions (channel_instance_id)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_channel_sessions_external_user_id "
            "ON channel_sessions (external_user_id)"
        )
    )
