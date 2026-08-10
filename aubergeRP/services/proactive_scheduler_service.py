"""ProactiveScheduler — transport-neutral proactive behavior engine."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..db_models import ScheduleInstanceRow
from ..services.delivery_service import make_delivery_adapter

if TYPE_CHECKING:
    from ..models.character import CharacterCard, ScheduleDefinition
    from ..services.schedule_instance_service import ScheduleInstanceService

logger = logging.getLogger(__name__)


def _build_proactive_injection(local_time_str: str, instruction: str) -> str:
    from .prompt_service import get_prompt

    template = get_prompt("proactive_event")
    return template.replace("{{local_time}}", local_time_str).replace("{{instruction}}", instruction)


@dataclass(slots=True)
class ProactiveDecision:
    action: str
    message: str = ""
    reason: str = ""


class ProactiveScheduler:
    def __init__(self, data_dir: Path | str, poll_interval: int = 60) -> None:
        self._data_dir = Path(data_dir)
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="proactive-scheduler")
            logger.info("ProactiveScheduler started (poll_interval=%ds)", self._poll_interval)

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        try:
            self._recover_startup_locks()
            await self._tick()
        except Exception:
            logger.exception("ProactiveScheduler: unhandled error during initial tick")
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._tick()
            except Exception:
                logger.exception("ProactiveScheduler: unhandled error during tick")

    async def _tick(self, utc_now: datetime | None = None) -> None:
        from ..services.schedule_instance_service import ScheduleInstanceService

        now = utc_now or datetime.now(UTC)
        svc = ScheduleInstanceService(self._data_dir)
        for row in svc.list_due(now):
            await self._process_instance(row, svc, now)

    def _recover_startup_locks(self, utc_now: datetime | None = None) -> None:
        from ..services.schedule_instance_service import ScheduleInstanceService

        svc = ScheduleInstanceService(self._data_dir)
        released = svc.release_startup_generation_locks(utc_now=utc_now)
        if released:
            logger.warning("ProactiveScheduler: released %d abandoned generation lock(s)", released)

    async def _process_instance(
        self,
        row: ScheduleInstanceRow,
        svc: "ScheduleInstanceService",
        utc_now: datetime,
    ) -> None:
        from ..services.character_service import CharacterNotFoundError, CharacterService

        if not svc.claim_for_generation(row.id, utc_now=utc_now):
            return
        try:
            char_svc = CharacterService(data_dir=self._data_dir)
            try:
                char = char_svc.get_character(row.character_id)
            except (CharacterNotFoundError, KeyError):
                svc.mark_failed(row.id, "character not found", utc_now=utc_now)
                return

            defn = svc.get_definition_for_row(row) or _get_schedule_definition(char, row.schedule_def_id)
            if defn is None:
                svc.mark_failed(row.id, "schedule definition not found", utc_now=utc_now)
                return
            if not defn.enabled:
                svc.complete_execution(row.id, defn, status="skipped", reason="disabled", utc_now=utc_now)
                return

            if row.last_sent_at is not None and row.minimum_cooldown_minutes > 0:
                last_sent = row.last_sent_at.replace(tzinfo=UTC)
                if utc_now < last_sent + timedelta(minutes=row.minimum_cooldown_minutes):
                    svc.complete_execution(
                        row.id,
                        defn,
                        status="skipped",
                        reason="cooldown",
                        utc_now=utc_now,
                    )
                    return

            zi = ZoneInfo(row.timezone)
            local_time_str = utc_now.astimezone(zi).strftime("%Y-%m-%d %H:%M ") + row.timezone
            injection = _build_proactive_injection(local_time_str, defn.instruction)

            message: str | None = None
            if row.decision_mode == "contextual":
                decision = await self._decide_send_or_skip(conversation_id=row.conversation_id, injection=injection)
                if decision.action == "skip":
                    svc.complete_execution(
                        row.id,
                        defn,
                        status="skipped",
                        reason=decision.reason or "contextual_skip",
                        utc_now=utc_now,
                    )
                    return
                message = decision.message.strip() or None

            if message is None:
                message = await self._generate(conversation_id=row.conversation_id, proactive_injection=injection)
            if message is None:
                svc.mark_failed(row.id, "generation_failed", utc_now=utc_now)
                return

            await self._persist_assistant_message(row.conversation_id, message)
            adapter = make_delivery_adapter(row.channel, self._data_dir)
            try:
                await adapter.deliver(
                    channel_instance_id=row.channel_instance_id,
                    external_chat_id=row.external_chat_id,
                    message_text=message,
                )
            except Exception:
                logger.exception(
                    "ProactiveScheduler: delivery failed for instance %s (message persisted)",
                    row.id,
                )

            svc.complete_execution(row.id, defn, status="sent", utc_now=utc_now, mark_sent=True)
        except Exception:
            logger.exception("ProactiveScheduler: unexpected error for instance %s", row.id)
            svc.mark_failed(row.id, "unexpected_error", utc_now=utc_now)

    async def _persist_assistant_message(self, conversation_id: str, message: str) -> None:
        from ..services.character_service import CharacterService
        from ..services.conversation_service import ConversationService

        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv_svc.append_message(conversation_id, "assistant", message, images=[])

    async def _decide_send_or_skip(self, *, conversation_id: str, injection: str) -> ProactiveDecision:
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.chat_service import build_prompt
        from ..services.conversation_service import ConversationService
        from ..services.prompt_service import get_prompt
        from ..services.summarization_service import maybe_summarize

        config = get_config()
        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv = conv_svc.get_conversation(conversation_id)
        char = char_svc.get_character(conv.character_id)
        manager = ConnectorManager(data_dir=self._data_dir, config=config)
        text_connector = manager.get_active_text_connector()
        if text_connector is None:
            return ProactiveDecision(action="skip", reason="no_active_text_connector")

        decision_instruction = get_prompt("proactive_decision")
        payload_prompt = f"{injection}\n\n{decision_instruction}"
        messages = build_prompt(
            conv,
            char,
            user_name=config.user.name,
            use_tool_calling=False,
            ooc_guardrail=False,
            proactive_injection=payload_prompt,
        )
        messages = await maybe_summarize(
            messages,
            text_connector,
            config.chat.context_window,
            config.chat.summarization_threshold,
        )
        chunks: list[str] = []
        async for token in text_connector.stream_chat_completion(messages):
            chunks.append(token)
        raw = "".join(chunks).strip()
        if not raw:
            return ProactiveDecision(action="skip", reason="empty_decision")

        obj = _extract_json_object(raw)
        if obj is None:
            return ProactiveDecision(action="send", message=raw)
        action = str(obj.get("action", "")).strip().lower()
        if action == "skip":
            return ProactiveDecision(action="skip", reason=str(obj.get("reason", "")).strip())
        if action == "send":
            return ProactiveDecision(
                action="send",
                message=str(obj.get("message", "")).strip(),
                reason=str(obj.get("reason", "")).strip(),
            )
        return ProactiveDecision(action="send", message=raw)

    async def _generate(self, *, conversation_id: str, proactive_injection: str) -> str | None:
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.chat_service import build_prompt
        from ..services.conversation_service import ConversationService
        from ..services.summarization_service import maybe_summarize

        config = get_config()
        data_dir = self._data_dir
        char_svc = CharacterService(data_dir=data_dir)
        conv_svc = ConversationService(data_dir=data_dir, character_service=char_svc)
        conv = conv_svc.get_conversation(conversation_id)
        char = char_svc.get_character(conv.character_id)
        connector_manager = ConnectorManager(data_dir=data_dir, config=config)
        text_connector = connector_manager.get_active_text_connector()
        if text_connector is None:
            return None
        messages = build_prompt(
            conv,
            char,
            user_name=config.user.name,
            use_tool_calling=False,
            ooc_guardrail=False,
            proactive_injection=proactive_injection,
        )
        try:
            messages = await maybe_summarize(
                messages,
                text_connector,
                config.chat.context_window,
                config.chat.summarization_threshold,
            )
            chunks: list[str] = []
            async for token in text_connector.stream_chat_completion(messages):
                chunks.append(token)
            text = "".join(chunks).strip()
            return text or None
        except Exception:
            logger.exception("ProactiveScheduler: generation failed for conversation %s", conversation_id)
            return None


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _get_schedule_definition(char: "CharacterCard", schedule_def_id: str) -> "ScheduleDefinition | None":
    from ..models.character import ScheduleDefinition

    ext = char.data.extensions.get("aubergerp", {})
    schedules_raw = ext.get("schedules", [])
    for raw in schedules_raw:
        if isinstance(raw, dict) and raw.get("id") == schedule_def_id:
            try:
                return ScheduleDefinition(**raw)
            except Exception:
                return None
    return None
