"""Migration 007 — add schedule_instances table."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schedule_instances (
                id TEXT PRIMARY KEY,
                schedule_def_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                channel_instance_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                external_chat_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                last_run_at TEXT,
                next_run_at TEXT,
                generation_started_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    )

    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_schedule_def_id "
            "ON schedule_instances (schedule_def_id)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_character_id "
            "ON schedule_instances (character_id)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_conversation_id "
            "ON schedule_instances (conversation_id)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_next_run_at "
            "ON schedule_instances (next_run_at)"
        )
    )
    # Unique: one row per (character, schedule_def, conversation)
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_instances_key "
            "ON schedule_instances (character_id, schedule_def_id, conversation_id)"
        )
    )
