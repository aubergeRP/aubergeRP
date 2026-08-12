"""Migration 011 — persisted conversation summaries.

Summaries used to be recomputed in memory on every turn and thrown away.
This table stores them so that a summary is produced once, reused on the
following turns, and extended incrementally from the previous one.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id TEXT NOT NULL PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                covers_until_message_id TEXT NOT NULL DEFAULT '',
                covers_message_count INTEGER NOT NULL DEFAULT 0,
                based_on_summary_id TEXT NOT NULL DEFAULT '',
                tokens INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_conversation_summaries_conversation_id "
            "ON conversation_summaries (conversation_id)"
        )
    )
