"""Migration 010 — observability metadata on llm_call_stats.

Adds the generation type (chat / proactive / summarization / image_prompt),
the model name reported by the connector, and a flag telling whether the token
counts came from the provider or from the local heuristic.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    cols = {
        row[1]  # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        for row in session.execute(text("PRAGMA table_info(llm_call_stats)")).all()
    }

    def add_if_missing(column_name: str, ddl: str) -> None:
        if column_name not in cols:
            session.execute(text(f"ALTER TABLE llm_call_stats ADD COLUMN {ddl}"))

    add_if_missing("generation_type", "generation_type TEXT NOT NULL DEFAULT 'chat'")
    add_if_missing("model", "model TEXT NOT NULL DEFAULT ''")
    add_if_missing("tokens_estimated", "tokens_estimated INTEGER NOT NULL DEFAULT 1")
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_llm_call_stats_created_at "
            "ON llm_call_stats (created_at)"
        )
    )
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_llm_call_stats_generation_type "
            "ON llm_call_stats (generation_type)"
        )
    )
