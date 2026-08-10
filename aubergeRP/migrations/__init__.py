"""Migration runner for aubergeRP.

Built-in migrations live in this package (``m{version:03d}_*.py``).
Users can add custom migrations by placing Python files in
``{data_dir}/custom_migrations/`` named with the same convention.  Each file
must expose a ``migrate(session: Session) -> None`` callable.

The current schema version is tracked in the ``schema_migrations`` table.
Migrations are applied in ascending version order, each inside its own
transaction, so a failure leaves the DB at the last successfully applied
version.
"""
from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, select

from aubergeRP.db_models import SchemaMigration

MigrateFunc = Callable[[Session], None]


# ---------------------------------------------------------------------------
# Built-in migration registry
# ---------------------------------------------------------------------------

def _builtin_migrations() -> dict[int, tuple[str, MigrateFunc]]:
    """Return {version: (description, migrate_fn)} for all built-in migrations."""
    from aubergeRP.migrations import (
        m001_initial,  # noqa: PLC0415
        m002_add_llm_call_stats,  # noqa: PLC0415
        m003_add_media_library,  # noqa: PLC0415
        m004_add_telegram,  # noqa: PLC0415
        m005_add_dialogue_only,  # noqa: PLC0415
        m006_add_timezone,  # noqa: PLC0415
        m007_add_schedules,  # noqa: PLC0415
        m008_add_webhook,  # noqa: PLC0415
        m009_proactive_engine,  # noqa: PLC0415
    )
    return {
        1: ("Initial schema and JSON import", m001_initial.migrate),
        2: ("Add LLM call statistics table", m002_add_llm_call_stats.migrate),
        3: ("Add media library table and backfill", m003_add_media_library.migrate),
        4: ("Add telegram_bots and channel_sessions tables", m004_add_telegram.migrate),
        5: ("Add dialogue_only column to telegram_bots", m005_add_dialogue_only.migrate),
        6: ("Add user_timezones table", m006_add_timezone.migrate),
        7: ("Add schedule_instances table", m007_add_schedules.migrate),
        8: ("Add webhook columns to telegram_bots", m008_add_webhook.migrate),
        9: ("Add proactive schedule engine fields", m009_proactive_engine.migrate),
    }


# ---------------------------------------------------------------------------
# Custom migration loader
# ---------------------------------------------------------------------------

def _load_custom_migrations(data_dir: str | Path) -> dict[int, tuple[str, MigrateFunc]]:
    """Load user-provided migration scripts from ``{data_dir}/custom_migrations/``.

    Each file is expected to be named ``m{version:03d}_{slug}.py`` and must
    expose a top-level ``migrate(session: Session) -> None`` function.
    """
    custom_dir = Path(data_dir) / "custom_migrations"
    if not custom_dir.exists():
        return {}

    result: dict[int, tuple[str, MigrateFunc]] = {}
    for path in sorted(custom_dir.glob("m[0-9][0-9][0-9]_*.py")):
        try:
            version = int(path.name[1:4])
        except ValueError:
            continue
        spec = importlib.util.spec_from_file_location(f"_custom_migration_{version}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"_custom_migration_{version}"] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "migrate"):
            continue
        description = getattr(module, "DESCRIPTION", path.stem)
        result[version] = (description, module.migrate)

    return result


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------

def run_migrations(engine: Engine, data_dir: str | Path = "data") -> None:
    """Apply all pending migrations (built-in + custom) in version order."""
    builtin = _builtin_migrations()
    custom = _load_custom_migrations(data_dir)

    # Custom migrations take precedence for the same version number only if
    # they explicitly replace a built-in; normally they have unique numbers.
    all_migrations: dict[int, tuple[str, MigrateFunc]] = {**builtin, **custom}

    with Session(engine) as session:
        applied = set(session.exec(select(SchemaMigration.version)).all())

    for version in sorted(all_migrations):
        if version in applied:
            continue
        description, migrate_fn = all_migrations[version]
        with Session(engine) as session:
            migrate_fn(session)
            session.add(SchemaMigration(
                version=version,
                description=description,
                applied_at=datetime.now(UTC),
            ))
            session.commit()
