"""Migration 005 — add dialogue_only column to telegram_bots."""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    # The column may already exist if the database was created from the updated
    # SQLModel metadata (e.g. fresh test databases).  Skip gracefully.
    try:
        session.execute(
            text(
                "ALTER TABLE telegram_bots ADD COLUMN dialogue_only INTEGER NOT NULL DEFAULT 0"
            )
        )
    except Exception as exc:
        if "already has a column" in str(exc).lower() or "duplicate column" in str(exc).lower():
            return
        raise
