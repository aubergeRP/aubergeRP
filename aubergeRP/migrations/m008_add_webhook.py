"""Migration 008 — add webhook columns to telegram_bots."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    existing = {
        row[1]
        for row in session.execute(text("PRAGMA table_info(telegram_bots)")).fetchall()
    }
    for col, default in [
        ("update_mode", "'polling'"),
        ("webhook_url", "''"),
        ("webhook_secret", "''"),
        ("webhook_last_error", "''"),
    ]:
        if col not in existing:
            session.execute(
                text(
                    f"ALTER TABLE telegram_bots "
                    f"ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                )
            )
