"""Migration 006 — add user_timezones table."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_timezones (
                id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                channel_instance_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                timezone TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    )

    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_user_timezones_channel "
            "ON user_timezones (channel)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_user_timezones_channel_instance_id "
            "ON user_timezones (channel_instance_id)"
        )
    )
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_timezones_user "
            "ON user_timezones (channel, channel_instance_id, external_user_id)"
        )
    )
