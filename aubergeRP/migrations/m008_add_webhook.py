"""Migration 008 — add webhook columns to telegram_bots."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    for col, default in [
        ("update_mode", "'polling'"),
        ("webhook_url", "''"),
        ("webhook_secret", "''"),
        ("webhook_last_error", "''"),
    ]:
        session.execute(
            text(
                f"ALTER TABLE telegram_bots "
                f"ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
            )
        )
