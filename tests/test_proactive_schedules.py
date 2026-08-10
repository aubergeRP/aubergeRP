"""Tests for proactive scheduling (Step 6).

Covers:
- Character card schedule import / export / unknown extensions preserved
- calc_next_run_at: daily_at, daily_window, timezone, DST
- ScheduleInstanceService CRUD, idempotency, completion
- ProactiveScheduler._tick: generation, delivery, idempotency, restart safety
- Disabled card and runtime-disabled schedules
- Telegram and Web use the same scheduler
- Generated message persisted normally
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from aubergeRP.database import get_engine
from aubergeRP.db_models import ScheduleInstanceRow
from aubergeRP.models.character import CharacterData, ScheduleDefinition
from aubergeRP.services.schedule_instance_service import (
    ScheduleInstanceService,
    calc_next_run_at,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    from aubergeRP.database import init_db
    init_db(tmp_path)
    return tmp_path


@pytest.fixture()
def svc(data_dir: Path) -> ScheduleInstanceService:
    return ScheduleInstanceService(data_dir)


def _make_defn(
    defn_id: str = "morning",
    defn_type: str = "daily_at",
    time: str | None = "09:00",
    start: str | None = None,
    end: str | None = None,
    enabled: bool = True,
    instruction: str = "Say good morning",
) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=id,
        enabled=enabled,
        type=type,  # type: ignore[arg-type]
        time=time,
        start=start,
        end=end,
        instruction=instruction,
    )


# ---------------------------------------------------------------------------
# calc_next_run_at
# ---------------------------------------------------------------------------


class TestCalcNextRunAt:
    def test_daily_at_future_today(self) -> None:
        # 07:00 UTC Paris = 09:00 → schedule is at 10:00 local → should be today
        tz = "Europe/Paris"
        now = datetime(2026, 6, 10, 7, 0, tzinfo=UTC)  # 09:00 Paris (CEST=+2)
        defn = _make_defn(type="daily_at", time="10:00")
        result = calc_next_run_at(defn, tz, utc_now=now)
        local = result.astimezone(ZoneInfo(tz))
        assert local.hour == 10
        assert local.minute == 0
        assert local.date() == (now.astimezone(ZoneInfo(tz))).date()

    def test_daily_at_past_today_goes_tomorrow(self) -> None:
        tz = "Europe/Paris"
        now = datetime(2026, 6, 10, 10, 30, tzinfo=UTC)  # 12:30 Paris
        defn = _make_defn(type="daily_at", time="10:00")
        result = calc_next_run_at(defn, tz, utc_now=now)
        local = result.astimezone(ZoneInfo(tz))
        assert local.hour == 10
        assert local.date() > now.astimezone(ZoneInfo(tz)).date()

    def test_daily_at_utc(self) -> None:
        tz = "UTC"
        now = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        defn = _make_defn(type="daily_at", time="09:30")
        result = calc_next_run_at(defn, tz, utc_now=now)
        assert result.hour == 9
        assert result.minute == 30

    def test_daily_window_within_bounds(self) -> None:
        tz = "UTC"
        now = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)  # before window
        defn = _make_defn(defn_type="daily_window", time=None, start="09:00", end="11:00")
        result = calc_next_run_at(defn, tz, utc_now=now)
        local = result.astimezone(ZoneInfo(tz))
        assert 9 <= local.hour < 11

    def test_daily_window_past_window_goes_tomorrow(self) -> None:
        tz = "UTC"
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # after window
        defn = _make_defn(defn_type="daily_window", time=None, start="09:00", end="11:00")
        result = calc_next_run_at(defn, tz, utc_now=now)
        assert result.date() > now.date()

    def test_daily_window_runs_once_per_day(self) -> None:
        """After the window closes, the next trigger is scheduled for tomorrow."""
        tz = "UTC"
        now = datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
        defn = _make_defn(defn_type="daily_window", time=None, start="09:00", end="11:00")
        calc_next_run_at(defn, tz, utc_now=now)  # warm up; result not checked
        # Simulate: time has advanced past the window end (11:00)
        past_window = datetime(2026, 1, 1, 11, 30, tzinfo=UTC)
        r2 = calc_next_run_at(defn, tz, utc_now=past_window)
        assert r2.date() > past_window.date()

    def test_dst_spring_forward(self) -> None:
        """Europe/Paris spring forward: 2026-03-29 02:00 → 03:00."""
        tz = "Europe/Paris"
        # Just before DST; 00:30 UTC = 01:30 Paris (CET +1)
        now = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)
        defn = _make_defn(type="daily_at", time="09:00")
        result = calc_next_run_at(defn, tz, utc_now=now)
        local = result.astimezone(ZoneInfo(tz))
        assert local.hour == 9
        assert local.minute == 0

    def test_unknown_type_raises(self) -> None:
        defn = ScheduleDefinition(id="x", type="daily_at", instruction="x")
        defn = defn.model_copy(update={"type": "unknown"})  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown schedule type"):
            calc_next_run_at(defn, "UTC")

    def test_daily_at_missing_time_raises(self) -> None:
        defn = ScheduleDefinition(id="x", type="daily_at", time=None, instruction="x")
        with pytest.raises(ValueError, match="'time' field"):
            calc_next_run_at(defn, "UTC")

    def test_daily_window_bad_order_raises(self) -> None:
        defn = _make_defn(defn_type="daily_window", time=None, start="11:00", end="09:00")
        with pytest.raises(ValueError, match="'end' must be after 'start'"):
            calc_next_run_at(defn, "UTC")


# ---------------------------------------------------------------------------
# Character card schedule import / export
# ---------------------------------------------------------------------------


class TestCharacterCardSchedules:
    def _make_raw_card(self, schedules: list[dict]) -> dict:
        return {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "Alice",
                "description": "A helpful AI.",
                "extensions": {
                    "aubergerp": {"schedules": schedules},
                    "custom_ext": {"foo": "bar"},  # unknown extension — must be preserved
                },
            },
        }

    def test_import_preserves_schedules(self, data_dir: Path) -> None:
        from aubergeRP.services.character_service import CharacterService

        svc = CharacterService(data_dir=data_dir)
        raw = self._make_raw_card([
            {
                "id": "morning_checkin",
                "enabled": True,
                "type": "daily_window",
                "start": "09:00",
                "end": "11:00",
                "instruction": "Say good morning",
            }
        ])
        card = svc.import_character_json(json.dumps(raw).encode())
        ext = card.data.extensions.get("aubergerp", {})
        schedules = ext.get("schedules", [])
        assert len(schedules) == 1
        assert schedules[0]["id"] == "morning_checkin"

    def test_import_preserves_unknown_extensions(self, data_dir: Path) -> None:
        from aubergeRP.services.character_service import CharacterService

        svc = CharacterService(data_dir=data_dir)
        raw = self._make_raw_card([])
        card = svc.import_character_json(json.dumps(raw).encode())
        assert card.data.extensions.get("custom_ext", {}).get("foo") == "bar"

    def test_existing_cards_without_schedules_valid(self, data_dir: Path) -> None:
        from aubergeRP.services.character_service import CharacterService

        svc = CharacterService(data_dir=data_dir)
        raw = {
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {"name": "Bob", "description": "A simple character."},
        }
        card = svc.import_character_json(json.dumps(raw).encode())
        assert card.data.name == "Bob"
        # No schedules key — no error
        ext = card.data.extensions.get("aubergerp", {})
        assert ext.get("schedules", []) == [] or "schedules" not in ext

    def test_export_roundtrip(self, data_dir: Path) -> None:
        from aubergeRP.services.character_service import CharacterService

        svc = CharacterService(data_dir=data_dir)
        raw = self._make_raw_card([
            {"id": "evt1", "enabled": True, "type": "daily_at", "time": "10:00", "instruction": "Hi"}
        ])
        card = svc.import_character_json(json.dumps(raw).encode())
        exported = svc.export_character_json(card.id)
        re_imported = svc.import_character_json(json.dumps(exported).encode())
        ext = re_imported.data.extensions.get("aubergerp", {})
        assert ext.get("schedules", [{}])[0]["id"] == "evt1"

    def test_schedule_definition_model_validation(self) -> None:
        defn = ScheduleDefinition(
            id="my_sched",
            enabled=True,
            type="daily_window",
            start="08:00",
            end="10:00",
            instruction="Do something",
        )
        assert defn.type == "daily_window"
        assert defn.start == "08:00"


# ---------------------------------------------------------------------------
# ScheduleInstanceService
# ---------------------------------------------------------------------------


class TestScheduleInstanceService:
    def test_get_or_create_new(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, created = svc.get_or_create(
            defn=defn,
            character_id="char1",
            conversation_id="conv1",
            channel="web",
            channel_instance_id="web",
            external_user_id="user1",
            external_chat_id="",
            timezone="UTC",
        )
        assert created is True
        assert inst.schedule_def_id == "morning"
        assert inst.enabled is True
        assert inst.next_run_at is not None

    def test_get_or_create_idempotent(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        inst2, created = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        assert created is False

    def test_independent_instances_per_conversation(self, svc: ScheduleInstanceService) -> None:
        """Same character+schedule, different conversations → independent instances."""
        defn = _make_defn()
        inst_a, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv_a",
            channel="telegram", channel_instance_id="bot1", external_user_id="u1",
            external_chat_id="100", timezone="UTC",
        )
        inst_b, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv_b",
            channel="web", channel_instance_id="web", external_user_id="u2",
            external_chat_id="", timezone="UTC",
        )
        assert inst_a.id != inst_b.id

    def test_set_enabled_false(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        updated = svc.set_enabled(inst.id, False)
        assert updated.enabled is False

    def test_claim_for_generation(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        # Manually set next_run_at to the past
        _force_next_run_past(svc, inst.id)

        claimed = svc.claim_for_generation(inst.id)
        assert claimed is True
        # Second claim should fail
        claimed2 = svc.claim_for_generation(inst.id)
        assert claimed2 is False

    def test_complete_generation_advances_schedule(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        svc.claim_for_generation(inst.id)

        now = datetime.now(UTC)
        svc.complete_generation(inst.id, defn, utc_now=now)
        refreshed = svc.get_instance(inst.id)
        assert refreshed.last_run_at is not None
        assert refreshed.next_run_at is not None
        assert refreshed.next_run_at > now

    def test_release_lock_on_failure(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        svc.claim_for_generation(inst.id)
        svc.release_generation_lock(inst.id)
        # Can be claimed again
        assert svc.claim_for_generation(inst.id) is True

    def test_find_due_excludes_locked(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        due = svc.find_due()
        assert any(r.id == inst.id for r in due)

        svc.claim_for_generation(inst.id)
        due2 = svc.find_due()
        assert all(r.id != inst.id for r in due2)

    def test_find_due_excludes_disabled(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        svc.set_enabled(inst.id, False)
        due = svc.find_due()
        assert all(r.id != inst.id for r in due)

    def test_timezone_change_recalculates_next_run(self, svc: ScheduleInstanceService) -> None:
        defn = _make_defn(type="daily_at", time="09:00")
        inst, _ = svc.get_or_create(
            defn=defn, character_id="char1", conversation_id="conv1",
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        old_next = inst.next_run_at

        updated = svc.update_timezone(inst.id, "America/New_York", defn)
        # New York is UTC-4/5 so 09:00 there is much later in UTC
        assert updated.timezone == "America/New_York"
        # next_run_at must differ from UTC-based one
        assert updated.next_run_at != old_next


def _force_next_run_past(svc: ScheduleInstanceService, instance_id: str) -> None:
    """Directly set next_run_at to the past (test helper)."""
    from sqlmodel import Session

    from aubergeRP.database import get_engine

    past = datetime(2000, 1, 1, 0, 0, 0)
    with Session(get_engine(svc._data_dir)) as session:
        row = session.get(ScheduleInstanceRow, instance_id)
        if row:
            row.next_run_at = past
            session.add(row)
            session.commit()


# ---------------------------------------------------------------------------
# ProactiveScheduler
# ---------------------------------------------------------------------------


class TestProactiveScheduler:
    def _make_char(self, data_dir: Path, schedules: list[dict]) -> Any:
        from aubergeRP.services.character_service import CharacterService

        svc = CharacterService(data_dir=data_dir)
        return svc.create_character(CharacterData(
            name="Alice",
            description="Test character",
            extensions={"aubergerp": {"schedules": schedules}},
        ))

    def _make_conversation(self, data_dir: Path, character_id: str) -> Any:
        from aubergeRP.services.character_service import CharacterService
        from aubergeRP.services.conversation_service import ConversationService

        char_svc = CharacterService(data_dir=data_dir)
        conv_svc = ConversationService(data_dir=data_dir, character_service=char_svc)
        return conv_svc.create_conversation(character_id=character_id)

    @pytest.mark.asyncio
    async def test_tick_generates_and_persists(self, data_dir: Path) -> None:
        """A due schedule fires generation and the message is persisted."""
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "morning", "enabled": True, "type": "daily_at", "time": "09:00",
             "instruction": "Say good morning"}
        ])
        conv = self._make_conversation(data_dir, char.id)

        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="morning")
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)

        scheduler = ProactiveScheduler(data_dir)
        with patch.object(scheduler, "_generate", new_callable=AsyncMock, return_value="Good morning!"):
            from aubergeRP.services.delivery_service import WebDeliveryAdapter
            with patch("aubergeRP.services.proactive_scheduler_service.make_delivery_adapter",
                       return_value=WebDeliveryAdapter()):
                await scheduler._tick()

        refreshed = svc.get_instance(inst.id)
        assert refreshed.last_run_at is not None
        # next_run_at should be in the future
        assert refreshed.next_run_at is not None
        # Lock should be cleared
        with Session(get_engine(data_dir)) as s:
            row = s.get(ScheduleInstanceRow, inst.id)
            assert row is not None
            assert row.generation_started_at is None

    @pytest.mark.asyncio
    async def test_tick_does_not_regenerate_on_delivery_failure(self, data_dir: Path) -> None:
        """Generation succeeded → delivery failed → no duplicate generation."""
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": True, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="m")
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="telegram", channel_instance_id="bot1", external_user_id="u1",
            external_chat_id="100", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)

        generate_call_count = 0

        async def fake_generate(**kwargs: Any) -> str:
            nonlocal generate_call_count
            generate_call_count += 1
            return "Hi there!"

        failing_adapter = AsyncMock()
        failing_adapter.deliver = AsyncMock(side_effect=RuntimeError("Network error"))

        scheduler = ProactiveScheduler(data_dir)
        with (
            patch.object(scheduler, "_generate", side_effect=fake_generate),
            patch("aubergeRP.services.proactive_scheduler_service.make_delivery_adapter",
                  return_value=failing_adapter),
        ):
            await scheduler._tick()

        assert generate_call_count == 1
        # Schedule should still advance (delivery failure does not prevent next trigger)
        refreshed = svc.get_instance(inst.id)
        assert refreshed.last_run_at is not None

    @pytest.mark.asyncio
    async def test_restart_does_not_duplicate(self, data_dir: Path) -> None:
        """If generation_started_at is set from a previous run, release and re-try on next tick."""
        from sqlmodel import Session

        from aubergeRP.database import get_engine
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": True, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="m")
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)

        # Simulate a stale lock from a previous server run
        with Session(get_engine(data_dir)) as s:
            row = s.get(ScheduleInstanceRow, inst.id)
            if row:
                row.generation_started_at = datetime(2000, 1, 1)
                s.add(row)
                s.commit()

        # Tick should not find this row (it's locked) and not generate
        scheduler = ProactiveScheduler(data_dir)
        generate_called = False

        async def fake_generate(**kwargs: Any) -> str:
            nonlocal generate_called
            generate_called = True
            return "Hi"

        with (
            patch.object(scheduler, "_generate", side_effect=fake_generate),
            patch("aubergeRP.services.proactive_scheduler_service.make_delivery_adapter",
                  return_value=AsyncMock()),
        ):
            await scheduler._tick()

        assert generate_called is False

    @pytest.mark.asyncio
    async def test_disabled_card_schedule_does_not_run(self, data_dir: Path) -> None:
        """Card-level disabled schedule: if defn.enabled is False, skip."""

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": False, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(id="m", enabled=False)
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        # Card defn is disabled → instance should also be created with enabled=False
        assert inst.enabled is False

        # find_due excludes disabled instances
        due = svc.find_due()
        assert all(r.id != inst.id for r in due)

    @pytest.mark.asyncio
    async def test_runtime_disabled_does_not_run(self, data_dir: Path) -> None:
        """Runtime-disabled instance (enabled=False) is skipped by scheduler."""
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": True, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="m")
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)
        svc.set_enabled(inst.id, False)  # Runtime override

        generate_called = False

        async def fake_generate(**kwargs: Any) -> str:
            nonlocal generate_called
            generate_called = True
            return "Hi"

        scheduler = ProactiveScheduler(data_dir)
        with patch.object(scheduler, "_generate", side_effect=fake_generate):
            await scheduler._tick()

        assert generate_called is False

    @pytest.mark.asyncio
    async def test_telegram_and_web_use_same_scheduler(self, data_dir: Path) -> None:
        """Two instances (one Telegram, one Web) for same schedule both fire."""
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": True, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv_web = self._make_conversation(data_dir, char.id)
        conv_tg  = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="m")

        inst_web, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv_web.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        inst_tg, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv_tg.id,
            channel="telegram", channel_instance_id="bot1", external_user_id="u2",
            external_chat_id="200", timezone="UTC",
        )
        _force_next_run_past(svc, inst_web.id)
        _force_next_run_past(svc, inst_tg.id)

        generated_for = []

        async def fake_generate(*, conversation_id: str, **kwargs: Any) -> str:
            generated_for.append(conversation_id)
            return "Hi"

        scheduler = ProactiveScheduler(data_dir)
        with patch.object(scheduler, "_generate", side_effect=fake_generate):
            from aubergeRP.services.delivery_service import WebDeliveryAdapter
            with patch("aubergeRP.services.proactive_scheduler_service.make_delivery_adapter",
                       return_value=WebDeliveryAdapter()):
                await scheduler._tick()

        assert conv_web.id in generated_for
        assert conv_tg.id in generated_for

    @pytest.mark.asyncio
    async def test_generation_failure_releases_lock(self, data_dir: Path) -> None:
        """If generation raises, the lock is released so the next tick can retry."""
        from aubergeRP.services.proactive_scheduler_service import ProactiveScheduler

        char = self._make_char(data_dir, [
            {"id": "m", "enabled": True, "type": "daily_at", "time": "09:00", "instruction": "Hi"}
        ])
        conv = self._make_conversation(data_dir, char.id)
        svc = ScheduleInstanceService(data_dir)
        defn = _make_defn(defn_id="m")
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )
        _force_next_run_past(svc, inst.id)

        async def failing_generate(**kwargs: Any) -> None:
            return None  # Simulate None return (failure path in _generate)

        scheduler = ProactiveScheduler(data_dir)
        with patch.object(scheduler, "_generate", side_effect=failing_generate):
            await scheduler._tick()

        # Lock must be released
        from sqlmodel import Session

        from aubergeRP.database import get_engine
        with Session(get_engine(data_dir)) as s:
            row = s.get(ScheduleInstanceRow, inst.id)
            assert row is not None
            assert row.generation_started_at is None


# ---------------------------------------------------------------------------
# ChatService proactive injection
# ---------------------------------------------------------------------------


class TestChatServiceProactiveInjection:
    def test_build_prompt_includes_proactive_injection(self) -> None:
        """build_prompt appends the proactive injection as a late system message."""
        from aubergeRP.models.character import CharacterCard, CharacterData
        from aubergeRP.models.conversation import Conversation
        from aubergeRP.services.chat_service import build_prompt

        char = CharacterCard(
            id="char1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data=CharacterData(name="Alice", description="A character"),
        )
        conv = Conversation(
            id="conv1",
            character_id="char1",
            character_name="Alice",
            title="Test",
            messages=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        injection = "[Proactive event]\nSay hello"
        messages = build_prompt(conv, char, proactive_injection=injection)
        last = messages[-1]
        assert last["role"] == "system"
        assert "[Proactive event]" in last["content"]

    def test_build_prompt_no_injection_by_default(self) -> None:
        from aubergeRP.models.character import CharacterCard, CharacterData
        from aubergeRP.models.conversation import Conversation
        from aubergeRP.services.chat_service import build_prompt

        char = CharacterCard(
            id="char1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            data=CharacterData(name="Alice", description="A character"),
        )
        conv = Conversation(
            id="conv1",
            character_id="char1",
            character_name="Alice",
            title="Test",
            messages=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        messages = build_prompt(conv, char)
        # Should not contain proactive injection
        assert all("[Proactive event]" not in m.get("content", "") for m in messages)


# ---------------------------------------------------------------------------
# Delivery service
# ---------------------------------------------------------------------------


class TestDeliveryService:
    @pytest.mark.asyncio
    async def test_web_adapter_is_noop(self) -> None:
        from aubergeRP.services.delivery_service import WebDeliveryAdapter

        adapter = WebDeliveryAdapter()
        # Should not raise
        await adapter.deliver(
            channel_instance_id="web",
            external_chat_id="user1",
            message_text="Hello",
        )

    def test_make_delivery_adapter_web(self, data_dir: Path) -> None:
        from aubergeRP.services.delivery_service import WebDeliveryAdapter, make_delivery_adapter

        adapter = make_delivery_adapter("web", data_dir)
        assert isinstance(adapter, WebDeliveryAdapter)

    def test_make_delivery_adapter_unknown_channel(self, data_dir: Path) -> None:
        from aubergeRP.services.delivery_service import WebDeliveryAdapter, make_delivery_adapter

        adapter = make_delivery_adapter("matrix", data_dir)
        assert isinstance(adapter, WebDeliveryAdapter)

    def test_make_delivery_adapter_telegram(self, data_dir: Path) -> None:
        from aubergeRP.services.delivery_service import TelegramDeliveryAdapter, make_delivery_adapter

        adapter = make_delivery_adapter("telegram", data_dir)
        assert isinstance(adapter, TelegramDeliveryAdapter)


# ---------------------------------------------------------------------------
# API router (basic smoke)
# ---------------------------------------------------------------------------


class TestSchedulesRouter:
    @pytest.mark.asyncio
    async def test_list_for_character_empty(self, data_dir: Path) -> None:
        from httpx import ASGITransport, AsyncClient

        import aubergeRP.main as main_mod

        app = main_mod.create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/schedules/instances/character/nonexistent")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_create_and_get_instance(self, data_dir: Path) -> None:
        import os

        from httpx import ASGITransport, AsyncClient

        import aubergeRP.main as main_mod
        from aubergeRP.services.character_service import CharacterService
        from aubergeRP.services.conversation_service import ConversationService

        app_data_dir = Path(os.environ["AUBERGE_DATA_DIR"])
        app_data_dir.mkdir(parents=True, exist_ok=True)
        from aubergeRP.database import init_db
        init_db(app_data_dir)

        char_svc = CharacterService(data_dir=app_data_dir)
        char = char_svc.create_character(CharacterData(name="Alice", description="Test"))
        conv_svc = ConversationService(data_dir=app_data_dir, character_service=char_svc)
        conv = conv_svc.create_conversation(character_id=char.id)

        app = main_mod.create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/schedules/instances", json={
                "schedule_def": {
                    "id": "morning",
                    "enabled": True,
                    "type": "daily_at",
                    "time": "09:00",
                    "instruction": "Say hello",
                },
                "character_id": char.id,
                "conversation_id": conv.id,
                "channel": "web",
                "channel_instance_id": "web",
                "external_user_id": "u1",
                "external_chat_id": "",
                "timezone": "UTC",
            })
        assert resp.status_code == 201
        body = resp.json()
        assert body["schedule_def_id"] == "morning"

    @pytest.mark.asyncio
    async def test_patch_enabled(self, data_dir: Path) -> None:
        import os

        from httpx import ASGITransport, AsyncClient

        import aubergeRP.main as main_mod
        from aubergeRP.services.character_service import CharacterService
        from aubergeRP.services.conversation_service import ConversationService

        # Use the same data_dir as the app (set by autouse fixture)
        app_data_dir = Path(os.environ["AUBERGE_DATA_DIR"])
        app_data_dir.mkdir(parents=True, exist_ok=True)
        from aubergeRP.database import init_db
        init_db(app_data_dir)

        char_svc = CharacterService(data_dir=app_data_dir)
        char = char_svc.create_character(CharacterData(name="Alice", description="Test"))
        conv_svc = ConversationService(data_dir=app_data_dir, character_service=char_svc)
        conv = conv_svc.create_conversation(character_id=char.id)

        svc = ScheduleInstanceService(app_data_dir)
        defn = _make_defn()
        inst, _ = svc.get_or_create(
            defn=defn, character_id=char.id, conversation_id=conv.id,
            channel="web", channel_instance_id="web", external_user_id="u1",
            external_chat_id="", timezone="UTC",
        )

        app = main_mod.create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(f"/api/schedules/instances/{inst.id}/enabled",
                                      json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


