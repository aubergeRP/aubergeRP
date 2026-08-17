"""Migration 012 — record the full image generation pipeline per media.

The media library only kept a single ``prompt`` column, and depending on the
code path it held either the raw ``[IMG:…]`` keywords or the final connector
prompt.  That made it impossible to tell from the admin whether the character
prefix and negative prompt were applied at all.

Each generation step is now stored separately.  Existing rows keep their
``prompt`` and get empty values for the new columns.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlmodel import Session


def migrate(session: Session) -> None:
    cols = {
        row[1]  # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        for row in session.execute(text("PRAGMA table_info(media_library)")).all()
    }

    for column_name in (
        "raw_prompt",
        "llm_input_prompt",
        "llm_output_prompt",
        "prompt_prefix",
        "negative_prompt",
        "connector_name",
    ):
        if column_name not in cols:
            session.execute(
                text(
                    f"ALTER TABLE media_library ADD COLUMN "
                    f"{column_name} TEXT NOT NULL DEFAULT ''"
                )
            )
