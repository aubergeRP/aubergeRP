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
                trigger_type TEXT NOT NULL DEFAULT 'daily_at',
                origin TEXT NOT NULL DEFAULT 'character-card',
                schedule_json TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL DEFAULT '',
                character_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                channel_instance_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                external_chat_id TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                last_run_at TEXT,
                last_sent_at TEXT,
                last_execution_at TEXT,
                last_execution_status TEXT NOT NULL DEFAULT '',
                last_execution_reason TEXT NOT NULL DEFAULT '',
                next_run_at TEXT,
                generation_started_at TEXT,
                decision_mode TEXT NOT NULL DEFAULT 'contextual',
                minimum_cooldown_minutes INTEGER NOT NULL DEFAULT 0,
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
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_trigger_type "
            "ON schedule_instances (trigger_type)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_origin "
            "ON schedule_instances (origin)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_schedule_instances_dedupe_key "
            "ON schedule_instances (dedupe_key)"
        )
    )
    # Unique: one row per (character, schedule_def, conversation)
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_schedule_instances_key "
            "ON schedule_instances (character_id, schedule_def_id, conversation_id)"
        )
    )
