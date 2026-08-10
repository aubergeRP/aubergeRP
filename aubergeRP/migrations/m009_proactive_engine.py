"""Migration 009 — proactive behavior engine fields for schedule_instances."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    cols = {
        row[1]  # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        for row in session.execute(text("PRAGMA table_info(schedule_instances)")).all()
    }

    def add_if_missing(column_name: str, ddl: str) -> None:
        if column_name not in cols:
            session.execute(text(f"ALTER TABLE schedule_instances ADD COLUMN {ddl}"))

    add_if_missing("trigger_type", "trigger_type TEXT NOT NULL DEFAULT 'daily_at'")
    add_if_missing("origin", "origin TEXT NOT NULL DEFAULT 'character-card'")
    add_if_missing("schedule_json", "schedule_json TEXT NOT NULL DEFAULT ''")
    add_if_missing("dedupe_key", "dedupe_key TEXT NOT NULL DEFAULT ''")
    add_if_missing("last_sent_at", "last_sent_at TEXT")
    add_if_missing("last_execution_at", "last_execution_at TEXT")
    add_if_missing("last_execution_status", "last_execution_status TEXT NOT NULL DEFAULT ''")
    add_if_missing("last_execution_reason", "last_execution_reason TEXT NOT NULL DEFAULT ''")
    add_if_missing("decision_mode", "decision_mode TEXT NOT NULL DEFAULT 'contextual'")
    add_if_missing("minimum_cooldown_minutes", "minimum_cooldown_minutes INTEGER NOT NULL DEFAULT 0")
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
