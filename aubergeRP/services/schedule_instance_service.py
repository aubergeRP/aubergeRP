"""ScheduleInstanceService — runtime execution state for proactive schedules.

Timezone / DST notes
--------------------
- ``next_run_at`` is stored as naive UTC in SQLite.
- ``daily_at`` and ``daily_window`` always recalculate from IANA timezone data,
  so DST transitions are handled without persisting fixed UTC offsets.
- ``after_delay`` and ``after_inactivity`` are anchored to user activity and
  re-based on each new user message.
"""
from __future__ import annotations

import json
import random
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import DateTime, bindparam, func, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, and_, select

from ..db_models import MessageRow, ScheduleInstanceRow
from ..models.character import ProactiveConfig, ScheduleDefinition

_DEFAULT_PROACTIVE = ProactiveConfig()


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got '{value}'")
    return int(parts[0]), int(parts[1])


def _normalize_schedule_definition(defn: ScheduleDefinition) -> ScheduleDefinition:
    if defn.type == "after_delay" and (defn.delay_minutes is None or defn.delay_minutes <= 0):
        raise ValueError("after_delay schedule requires positive 'delay_minutes'")
    if defn.type == "after_inactivity" and (
        defn.inactivity_minutes is None or defn.inactivity_minutes <= 0
    ):
        raise ValueError("after_inactivity schedule requires positive 'inactivity_minutes'")
    return defn


def _apply_not_before(candidate_utc: datetime, timezone: str, not_before_time: str | None) -> datetime:
    if not not_before_time:
        return candidate_utc
    hour, minute = _parse_hhmm(not_before_time)
    zi = ZoneInfo(timezone)
    local_candidate = candidate_utc.astimezone(zi)
    floor = local_candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_candidate < floor:
        return floor.astimezone(UTC)
    return candidate_utc


def calc_next_run_at(
    defn: ScheduleDefinition,
    timezone: str,
    *,
    utc_now: datetime | None = None,
    last_user_message_at: datetime | None = None,
) -> datetime:
    defn = _normalize_schedule_definition(defn)
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
        window_len = end_minutes - start_minutes
        offset = random.randint(0, window_len - 1)  # noqa: S311
        target_minutes = start_minutes + offset
        candidate = now_local.replace(
            hour=target_minutes // 60,
            minute=target_minutes % 60,
            second=0,
            microsecond=0,
        )
        if candidate <= now_local:
            candidate = (now_local + timedelta(days=1)).replace(
                hour=target_minutes // 60,
                minute=target_minutes % 60,
                second=0,
                microsecond=0,
            )
        return candidate.astimezone(UTC)

    if defn.type in {"after_delay", "after_inactivity"}:
        base = last_user_message_at or now_utc
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        minutes = defn.delay_minutes if defn.type == "after_delay" else defn.inactivity_minutes
        assert minutes is not None
        candidate = base + timedelta(minutes=minutes)
        return _apply_not_before(candidate, timezone, defn.not_before_time)

    raise ValueError(f"Unknown schedule type: {defn.type!r}")


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _ensure_utc(dt).astimezone(UTC).replace(tzinfo=None)


def _schedule_to_json(defn: ScheduleDefinition) -> str:
    return json.dumps(defn.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _schedule_from_json(raw: str) -> ScheduleDefinition | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        return ScheduleDefinition(**obj)
    except Exception:
        return None


def _dedupe_key(defn: ScheduleDefinition) -> str:
    payload = defn.model_dump(mode="json")
    payload.pop("id", None)
    payload.pop("enabled", None)
    payload["instruction"] = str(payload.get("instruction", "")).strip().lower()
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ScheduleInstancePublic(BaseModel):
    id: str
    schedule_def_id: str
    trigger_type: str
    origin: str
    schedule: dict[str, Any] | None
    dedupe_key: str
    character_id: str
    conversation_id: str
    channel: str
    channel_instance_id: str
    external_user_id: str
    external_chat_id: str
    enabled: bool
    timezone: str
    decision_mode: str
    minimum_cooldown_minutes: int
    last_run_at: datetime | None
    last_sent_at: datetime | None
    last_execution_at: datetime | None
    last_execution_status: str
    last_execution_reason: str
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduleInstanceService:
    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine

        return Session(get_engine(self._data_dir))

    def _to_public(self, row: ScheduleInstanceRow) -> ScheduleInstancePublic:
        return ScheduleInstancePublic(
            id=row.id,
            schedule_def_id=row.schedule_def_id,
            trigger_type=row.trigger_type,
            origin=row.origin,
            schedule=json.loads(row.schedule_json) if row.schedule_json else None,
            dedupe_key=row.dedupe_key,
            character_id=row.character_id,
            conversation_id=row.conversation_id,
            channel=row.channel,
            channel_instance_id=row.channel_instance_id,
            external_user_id=row.external_user_id,
            external_chat_id=row.external_chat_id,
            enabled=row.enabled,
            timezone=row.timezone,
            decision_mode=row.decision_mode,
            minimum_cooldown_minutes=row.minimum_cooldown_minutes,
            last_run_at=_ensure_utc(row.last_run_at) if row.last_run_at else None,
            last_sent_at=_ensure_utc(row.last_sent_at) if row.last_sent_at else None,
            last_execution_at=_ensure_utc(row.last_execution_at) if row.last_execution_at else None,
            last_execution_status=row.last_execution_status,
            last_execution_reason=row.last_execution_reason,
            next_run_at=_ensure_utc(row.next_run_at) if row.next_run_at else None,
            created_at=_ensure_utc(row.created_at),
            updated_at=_ensure_utc(row.updated_at),
        )

    def _get_last_user_message_at(self, conversation_id: str) -> datetime | None:
        with self._get_session() as session:
            dt = session.exec(
                select(func.max(MessageRow.timestamp)).where(
                    and_(MessageRow.conversation_id == conversation_id, MessageRow.role == "user")
                )
            ).one()
        if dt is None:
            return None
        return _ensure_utc(dt)

    def get_instance(self, instance_id: str) -> ScheduleInstancePublic:
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            return self._to_public(row)

    def get_instance_row(self, instance_id: str) -> ScheduleInstanceRow:
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            return row

    def list_for_character(self, character_id: str) -> list[ScheduleInstancePublic]:
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(ScheduleInstanceRow.character_id == character_id)
            ).all()
            return [self._to_public(r) for r in rows]

    def list_for_conversation(self, conversation_id: str) -> list[ScheduleInstancePublic]:
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(ScheduleInstanceRow.conversation_id == conversation_id)
            ).all()
            return [self._to_public(r) for r in rows]

    def list_due(self, utc_now: datetime | None = None) -> list[ScheduleInstanceRow]:
        now = (utc_now or datetime.now(UTC)).replace(tzinfo=None)
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.enabled == True,  # noqa: E712
                        ScheduleInstanceRow.next_run_at != None,  # noqa: E711
                        ScheduleInstanceRow.next_run_at <= now,  # type: ignore[operator]
                        ScheduleInstanceRow.generation_started_at == None,  # noqa: E711
                    )
                )
            ).all()
            return list(rows)

    def find_due(self, utc_now: datetime | None = None) -> list[ScheduleInstanceRow]:
        return self.list_due(utc_now)

    def _enforce_limits(
        self,
        session: Session,
        *,
        conversation_id: str,
        defn: ScheduleDefinition,
        proactive: ProactiveConfig,
    ) -> None:
        if defn.type in {"after_delay", "after_inactivity"}:
            horizon = defn.delay_minutes if defn.type == "after_delay" else defn.inactivity_minutes
            if horizon and horizon > proactive.maximum_scheduling_horizon_minutes:
                raise ValueError(
                    "Requested schedule exceeds maximum scheduling horizon "
                    f"({proactive.maximum_scheduling_horizon_minutes} minutes)"
                )
        active = session.exec(
            select(func.count()).where(
                and_(
                    ScheduleInstanceRow.conversation_id == conversation_id,
                    ScheduleInstanceRow.enabled == True,  # noqa: E712
                )
            )
        ).one()
        if int(active or 0) >= proactive.maximum_active_schedules_per_conversation:
            raise ValueError(
                "Maximum active schedules per conversation reached "
                f"({proactive.maximum_active_schedules_per_conversation})"
            )

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
        *,
        origin: str = "character-card",
        decision_mode: str = "always_send",
        proactive: ProactiveConfig | None = None,
    ) -> tuple[ScheduleInstancePublic, bool]:
        defn = _normalize_schedule_definition(defn)
        proactive_cfg = proactive or _DEFAULT_PROACTIVE
        now = datetime.now(UTC)
        dedupe = _dedupe_key(defn)
        schedule_json = _schedule_to_json(defn)
        min_cooldown = (
            defn.minimum_cooldown_minutes
            if defn.minimum_cooldown_minutes is not None
            else proactive_cfg.minimum_cooldown_minutes
        )

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

            duplicate = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.conversation_id == conversation_id,
                        ScheduleInstanceRow.dedupe_key == dedupe,
                        ScheduleInstanceRow.enabled == True,  # noqa: E712
                    )
                )
            ).first()
            if duplicate is not None:
                return self._to_public(duplicate), False

            self._enforce_limits(
                session,
                conversation_id=conversation_id,
                defn=defn,
                proactive=proactive_cfg,
            )

        last_user_message_at = self._get_last_user_message_at(conversation_id)
        next_run: datetime | None = None
        if defn.type in {"after_delay", "after_inactivity"} and last_user_message_at is None:
            next_run = None
        else:
            next_run = calc_next_run_at(
                defn,
                timezone,
                utc_now=now,
                last_user_message_at=last_user_message_at,
            )

        row = ScheduleInstanceRow(
            id=str(uuid.uuid4()),
            schedule_def_id=defn.id,
            trigger_type=defn.type,
            origin=origin,
            schedule_json=schedule_json,
            dedupe_key=dedupe,
            character_id=character_id,
            conversation_id=conversation_id,
            channel=channel,
            channel_instance_id=channel_instance_id,
            external_user_id=external_user_id,
            external_chat_id=external_chat_id,
            enabled=defn.enabled,
            timezone=timezone,
            decision_mode=decision_mode,
            minimum_cooldown_minutes=min_cooldown,
            next_run_at=_to_naive_utc(next_run),
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
                if existing is not None:
                    return self._to_public(existing), False
            return self._to_public(row), False

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

    def get_definition_for_row(self, row: ScheduleInstanceRow) -> ScheduleDefinition | None:
        return _schedule_from_json(row.schedule_json)

    def update_timezone(
        self,
        instance_id: str,
        timezone: str,
        defn: ScheduleDefinition,
    ) -> ScheduleInstancePublic:
        now = datetime.now(UTC)
        last_user_message_at: datetime | None = None
        if defn.type in {"after_delay", "after_inactivity"}:
            row_public = self.get_instance(instance_id)
            last_user_message_at = self._get_last_user_message_at(row_public.conversation_id)
        next_run = (
            calc_next_run_at(defn, timezone, utc_now=now, last_user_message_at=last_user_message_at)
            if not (defn.type in {"after_delay", "after_inactivity"} and last_user_message_at is None)
            else None
        )
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                raise KeyError(instance_id)
            row.timezone = timezone
            row.next_run_at = _to_naive_utc(next_run)
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()
            return self._to_public(row)

    def rebase_event_triggers_on_user_message(
        self,
        conversation_id: str,
        *,
        user_message_at: datetime | None = None,
    ) -> int:
        message_time = _ensure_utc(user_message_at or datetime.now(UTC))
        updated = 0
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.conversation_id == conversation_id,
                        ScheduleInstanceRow.enabled == True,  # noqa: E712
                    )
                )
            ).all()
            for row in rows:
                defn = _schedule_from_json(row.schedule_json)
                if defn is None or defn.type not in {"after_delay", "after_inactivity"}:
                    continue
                next_run = calc_next_run_at(
                    defn,
                    row.timezone,
                    utc_now=datetime.now(UTC),
                    last_user_message_at=message_time,
                )
                row.next_run_at = _to_naive_utc(next_run)
                row.generation_started_at = None
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(row)
                updated += 1
            session.commit()
        return updated

    def update_timezone_for_user(
        self,
        *,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        timezone: str,
    ) -> int:
        updated = 0
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(
                    and_(
                        ScheduleInstanceRow.channel == channel,
                        ScheduleInstanceRow.channel_instance_id == channel_instance_id,
                        ScheduleInstanceRow.external_user_id == external_user_id,
                    )
                )
            ).all()
            for row in rows:
                defn = _schedule_from_json(row.schedule_json)
                if defn is None:
                    continue
                last_user_message_at = (
                    self._get_last_user_message_at(row.conversation_id)
                    if defn.type in {"after_delay", "after_inactivity"}
                    else None
                )
                next_run = (
                    calc_next_run_at(
                        defn,
                        timezone,
                        utc_now=datetime.now(UTC),
                        last_user_message_at=last_user_message_at,
                    )
                    if not (defn.type in {"after_delay", "after_inactivity"} and last_user_message_at is None)
                    else None
                )
                row.timezone = timezone
                row.next_run_at = _to_naive_utc(next_run)
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                session.add(row)
                updated += 1
            session.commit()
        return updated

    def claim_for_generation(self, instance_id: str, utc_now: datetime | None = None) -> bool:
        from sqlalchemy.engine import CursorResult

        now = (utc_now or datetime.now(UTC)).replace(tzinfo=None)
        with self._get_session() as session:
            stmt = text(
                "UPDATE schedule_instances "
                "SET generation_started_at = :now, updated_at = :now "
                "WHERE id = :id AND generation_started_at IS NULL"
            ).bindparams(bindparam("now", type_=DateTime()), bindparam("id"))
            result: CursorResult[Any] = session.execute(stmt, {"now": now, "id": instance_id})  # type: ignore[assignment]
            session.commit()
            return result.rowcount == 1

    def release_startup_generation_locks(self, utc_now: datetime | None = None) -> int:
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

    def complete_execution(
        self,
        instance_id: str,
        defn: ScheduleDefinition,
        *,
        status: str,
        reason: str = "",
        utc_now: datetime | None = None,
        mark_sent: bool = False,
    ) -> None:
        now = utc_now or datetime.now(UTC)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                return
            row.last_run_at = now.replace(tzinfo=None)
            row.last_execution_at = now.replace(tzinfo=None)
            row.last_execution_status = status
            row.last_execution_reason = reason
            if mark_sent:
                row.last_sent_at = now.replace(tzinfo=None)

            if defn.one_shot and status in {"sent", "skipped"}:
                row.enabled = False
                row.next_run_at = None
            elif defn.type in {"after_delay", "after_inactivity"}:
                row.next_run_at = None
            else:
                next_run = calc_next_run_at(defn, row.timezone, utc_now=now)
                row.next_run_at = _to_naive_utc(next_run)

            row.generation_started_at = None
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()

    def complete_generation(
        self,
        instance_id: str,
        defn: ScheduleDefinition,
        utc_now: datetime | None = None,
    ) -> None:
        self.complete_execution(
            instance_id,
            defn,
            status="sent",
            utc_now=utc_now,
            mark_sent=True,
        )

    def mark_failed(
        self,
        instance_id: str,
        reason: str,
        *,
        utc_now: datetime | None = None,
    ) -> None:
        now = utc_now or datetime.now(UTC)
        with self._get_session() as session:
            row = session.get(ScheduleInstanceRow, instance_id)
            if row is None:
                return
            row.last_execution_at = now.replace(tzinfo=None)
            row.last_execution_status = "failed"
            row.last_execution_reason = reason
            row.generation_started_at = None
            row.updated_at = now.replace(tzinfo=None)
            session.add(row)
            session.commit()

    def release_generation_lock(self, instance_id: str) -> None:
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

    def delete_orphan_instances(self, existing_character_ids: Iterable[str]) -> int:
        """Delete instances whose character no longer exists. Returns the count."""
        known = set(existing_character_ids)
        with self._get_session() as session:
            rows = session.exec(select(ScheduleInstanceRow)).all()
            orphans = [r for r in rows if r.character_id not in known]
            for row in orphans:
                session.delete(row)
            if orphans:
                session.commit()
            return len(orphans)

    def delete_for_character(self, character_id: str) -> int:
        with self._get_session() as session:
            rows = session.exec(
                select(ScheduleInstanceRow).where(ScheduleInstanceRow.character_id == character_id)
            ).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)
