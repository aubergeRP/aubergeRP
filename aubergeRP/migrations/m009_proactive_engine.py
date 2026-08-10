"""Migration 009 — proactive behavior engine fields for schedule_instances."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'daily_at'"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN origin TEXT NOT NULL DEFAULT 'character-card'"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN schedule_json TEXT NOT NULL DEFAULT ''"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN last_sent_at TEXT"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN last_execution_at TEXT"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN last_execution_status TEXT NOT NULL DEFAULT ''"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN last_execution_reason TEXT NOT NULL DEFAULT ''"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN decision_mode TEXT NOT NULL DEFAULT 'contextual'"))
    session.execute(text("ALTER TABLE schedule_instances ADD COLUMN minimum_cooldown_minutes INTEGER NOT NULL DEFAULT 0"))
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
