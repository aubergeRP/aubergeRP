"""ScheduleInstanceService — runtime execution state for proactive schedules.

Each ``ScheduleDefinition`` inside a character card can produce one
``ScheduleInstanceRow`` per (character, schedule_def, conversation) triple.
This service handles CRUD and the next-run-time calculation.

Timezone / DST notes
--------------------
- ``next_run_at`` is always stored as UTC.
- ``daily_at`` schedules pick the user's next local "HH:MM" occurrence.
- ``daily_window`` schedules pick a random time within the local window.
- Both recalculate cleanly across DST boundaries because we convert from the
  IANA timezone each time — we never store fixed UTC offsets.
"""
from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, and_, select

from ..db_models import ScheduleInstanceRow
from ..models.character import ScheduleDefinition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse "HH:MM" → (hour, minute).  Raises ValueError for bad input."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got '{value}'")
    return int(parts[0]), int(parts[1])


def calc_next_run_at(
    defn: ScheduleDefinition,
    timezone: str,
    *,
    utc_now: datetime | None = None,
) -> datetime:
    """Return the next UTC trigger time for *defn* in *timezone*.

    Args:
        defn:     The schedule definition from the character card.
        timezone: IANA timezone string (e.g. "Europe/Paris").
        utc_now:  Reference UTC time; defaults to ``datetime.now(UTC)``.

    Raises:
        ValueError: For invalid schedule definitions or timezone strings.
    """
    now_utc = utc_now if utc_now is not None else datetime.now(UTC)
    zi = ZoneInfo(timezone)
    now_local = now_utc.astimezone(zi)

    if defn.type == "daily_at":
        if not defn.time:
            raise ValueError("daily_at schedule requires 'time' field")
        hour, minute = _parse_hhmm(defn.time)
        candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    if defn.type == "daily_window":
        if not defn.start or not defn.end:
            raise ValueError("daily_window schedule requires 'start' and 'end' fields")
        start_h, start_m = _parse_hhmm(defn.start)
        end_h, end_m = _parse_hhmm(defn.end)
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        if end_minutes <= start_minutes:
            raise ValueError("daily_window 'end' must be after 'start'")

        # Pick a random offset in [0, window_len) minutes
        window_len = end_minutes - start_minutes
        offset = random.randint(0, window_len - 1)  # noqa: S311 (not crypto)
        target_minutes = start_minutes + offset
        target_h = target_minutes // 60
        target_m = target_minutes % 60

        candidate = now_local.replace(
            hour=target_h, minute=target_m, second=0, microsecond=0
        )
        if candidate <= now_local:
            # Already past window for today; schedule for tomorrow
            candidate = (now_local + timedelta(days=1)).replace(
                hour=target_h, minute=target_m, second=0, microsecond=0
            )
        return candidate.astimezone(UTC)

    raise ValueError(f"Unknown schedule type: {defn.type!r}")


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Public models
# ---------------------------------------------------------------------------

class ScheduleInstancePublic(BaseModel):
    id: str
    schedule_def_id: str
    character_id: str
    conversation_id: str
    channel: str
    channel_instance_id: str
    external_user_id: str
    external_chat_id: str
    enabled: bool
    timezone: str
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ScheduleInstanceService:
    """CRUD for :class:`ScheduleInstanceRow`."""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    def _to_public(self, row: ScheduleInstanceRow) -> ScheduleInstancePublic:
        return ScheduleInstancePublic(
            id=row.id,
            schedule_def_id=row.schedule_def_id,
            character_id=row.character_id,
            conversation_id=row.conversation_id,
            channel=row.channel,
            channel_instance_id=row.channel_instance_id,
            external_user_id=row.external_user_id,
            external_chat_id=row.external_chat_id,
            enabled=row.enabled,
            timezone=row.timezone,
            last_run_at=_ensure_utc(row.last_run_at) if row.last_run_at else None,
            next_run_at=_ensure_utc(row.next_run_at) if row.next_run_at else None,
            created_at=_ensure_utc(row.created_at),
            updated_at=_ensure_utc(row.updated_at),
        )

    # ── Query helpers ─────────────────────────────────────────────────────────

    def get_instance(self, instance_id: str) -> ScheduleInstancePublic:
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            return self._to_public(row)

    def list_for_character(self, character_id: str) -> list[ScheduleInstancePublic]:
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    ScheduleInstanceRow.character_id == character_id
                )
            ).all()
            return [self._to_public(r) for r in rows]

    def list_for_conversation(self, conversation_id: str) -> list[ScheduleInstancePublic]:
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    ScheduleInstanceRow.conversation_id == conversation_id
                )
            ).all()
            return [self._to_public(r) for r in rows]

    def find_due(self, utc_now: datetime | None = None) -> list[ScheduleInstanceRow]:
        """Return all enabled rows whose next_run_at is in the past and not locked."""
        now = utc_now or datetime.now(UTC)
        # Store as naive UTC in SQLite — strip tzinfo for comparison
        now_naive = now.replace(tzinfo=None)
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.enabled == True,  # noqa: E712
                        ScheduleInstanceRow.next_run_at <= now_naive,  # type: ignore[operator]
                        ScheduleInstanceRow.generation_started_at == None,  # noqa: E711
                    )
                )
            ).all()
            return list(rows)

    # ── Write helpers ─────────────────────────────────────────────────────────

    def get_or_create(
        self,
        defn: ScheduleDefinition,
        character_id: str,
        conversation_id: str,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        external_chat_id: str,
        timezone: str,
    ) -> tuple[ScheduleInstancePublic, bool]:
        """Return (instance, created).

        Idempotent: returns the existing row if one already exists for the
        (character_id, schedule_def_id, conversation_id) triple.
        """
        now = datetime.now(UTC)
        with self._get_session() as session:
            existing = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.character_id == character_id,
                        ScheduleInstanceRow.schedule_def_id == defn.id,
                        ScheduleInstanceRow.conversation_id == conversation_id,
                    )
                )
            ).first()
            if existing is not None:
                return self._to_public(existing), False

        next_run = calc_next_run_at(defn, timezone, utc_now=now)
        row = ScheduleInstanceRow(
            id=str(uuid.uuid4()),
            schedule_def_id=defn.id,
            character_id=character_id,
            conversation_id=conversation_id,
            channel=channel,
            channel_instance_id=channel_instance_id,
            external_user_id=external_user_id,
            external_chat_id=external_chat_id,
            enabled=defn.enabled,
            timezone=timezone,
            next_run_at=next_run.replace(tzinfo=None),  # store naive UTC
            created_at=now.replace(tzinfo=None),
            updated_at=now.replace(tzinfo=None),
        )
        try:
            with self._get_session() as session:
                session.add(row)
                session.commit()
                session.refresh(row)
                return self._to_public(row), True
        except IntegrityError:
            with self._get_session() as session:
                existing = session.exec(
                    select(ScheduleInstanceRow).where(
                        and_(
                            ScheduleInstanceRow.character_id == character_id,
                            ScheduleInstanceRow.schedule_def_id == defn.id,
                            ScheduleInstanceRow.conversation_id == conversation_id,
                        )
                    )
                ).first()
                return self._to_public(existing) if existing else self._to_public(row), False

    def set_enabled(self, instance_id: str, enabled: bool) -> ScheduleInstancePublic:
        now = datetime.now(UTC)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            row.enabled = enabled
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()
            return self._to_public(row)

    def update_timezone(
        self,
        instance_id: str,
        timezone: str,
        defn: ScheduleDefinition,
    ) -> ScheduleInstancePublic:
        """Update the timezone and recalculate next_run_at."""
        now = datetime.now(UTC)
        next_run = calc_next_run_at(defn, timezone, utc_now=now)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            row.timezone = timezone
            row.next_run_at = next_run.replace(tzinfo=None)
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()
            return self._to_public(row)

    def claim_for_generation(self, instance_id: str, utc_now: datetime | None = None) -> bool:
        """Set generation_started_at atomically via UPDATE … WHERE IS NULL.

        Returns True if this call successfully acquired the claim (i.e. no
        concurrent claimer beat us to it).  Safe within a single SQLite writer
        because SQLite serialises writes; the affected-rows check additionally
        guards against a race if multiple processes share the same DB.
        """
        from sqlalchemy.engine import CursorResult

        now = (utc_now or datetime.now(UTC)).replace(tzinfo=None)
        with self._get_session() as session:
            stmt = text(
                "UPDATE schedule_instances"
                " SET generation_started_at = :now, updated_at = :now"
                " WHERE id = :id AND generation_started_at IS NULL"
            ).bindparams(bindparam("now", type_=DateTime()), bindparam("id"))
            result: CursorResult[Any] = session.execute(  # type: ignore[assignment]
                stmt,
                {"now": now, "id": instance_id},
            )
            session.commit()
            return result.rowcount == 1

    def release_startup_generation_locks(self, utc_now: datetime | None = None) -> int:
        """Clear generation locks left behind by a previous server process."""
        now = (utc_now or datetime.now(UTC)).replace(tzinfo=None)
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(text("generation_started_at IS NOT NULL"))
            ).all()
            for row in rows:
                row.generation_started_at = None
                row.updated_at = now
                session.add(row)
            session.commit()
            return len(rows)

    def complete_generation(
        self,
        instance_id: str,
        defn: ScheduleDefinition,
        utc_now: datetime | None = None,
    ) -> None:
        """Record a successful generation and advance next_run_at."""
        now = utc_now or datetime.now(UTC)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                return
            next_run = calc_next_run_at(defn, row.timezone, utc_now=now)
            row.last_run_at = now.replace(tzinfo=None)
            row.next_run_at = next_run.replace(tzinfo=None)
            row.generation_started_at = None
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()

    def release_generation_lock(self, instance_id: str) -> None:
        """Clear the generation lock without advancing the schedule (on failure)."""
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                return
            row.generation_started_at = None
            row.updated_at = now
            session.add(row)
            session.commit()

    def delete_instance(self, instance_id: str) -> None:
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            session.delete(row)
            session.commit()

    def delete_for_character(self, character_id: str) -> int:
        """Delete all instances for a character.  Returns number deleted."""
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    ScheduleInstanceRow.character_id == character_id
                )
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)
