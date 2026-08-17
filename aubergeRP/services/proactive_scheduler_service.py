"""ProactiveScheduler — transport-neutral proactive behavior engine."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..db_models import ScheduleInstanceRow
from ..services.delivery_service import make_delivery_adapter
from ..services.observability_service import get_registry, record_error
from ..services.summarization_service import effective_limits

if TYPE_CHECKING:
    from ..models.character import CharacterCard, ScheduleDefinition
    from ..services.schedule_instance_service import ScheduleInstanceService
    from ..services.statistics_service import StatisticsService

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

    def _statistics_service(self) -> StatisticsService | None:
        """Return a statistics recorder, or None if it cannot be built."""
        try:
            from ..services.statistics_service import StatisticsService

            return StatisticsService(data_dir=self._data_dir)
        except Exception:  # pragma: no cover - statistics must never break sending
            logger.debug("ProactiveScheduler: statistics unavailable", exc_info=True)
            return None

    @staticmethod
    def _record_llm_call(
        stats: StatisticsService | None,
        conversation_id: str,
        connector: Any,
        messages: list[dict[str, Any]],
        response_text: str,
        started: float,
        *,
        success: bool,
        error_detail: str = "",
    ) -> None:
        """Record a proactive generation in ``llm_call_stats``.

        Proactive generations bypass ChatService, so without this they were
        invisible in every usage figure.
        """
        if stats is None:
            return
        from ..services.summarization_service import _count_tokens, count_prompt_tokens

        usage = getattr(connector, "last_usage", None)
        if isinstance(usage, dict):
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))
            estimated = False
        else:
            tokens_in = count_prompt_tokens(messages)
            tokens_out = _count_tokens(response_text) if response_text else 0
            estimated = True
        try:
            stats.record_text_call(
                conversation_id=conversation_id,
                connector_id="",
                connector_name=type(connector).__name__,
                connector_backend=str(getattr(connector, "backend_id", "")),
                request_tokens=tokens_in,
                response_tokens=tokens_out,
                response_time_ms=int((perf_counter() - started) * 1000),
                success=success,
                error_detail=error_detail,
                generation_type="proactive",
                model=str(getattr(getattr(connector, "config", None), "model", "") or ""),
                tokens_estimated=estimated,
            )
        except Exception:  # pragma: no cover
            logger.debug("ProactiveScheduler: failed to record statistics", exc_info=True)

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
            self._purge_orphan_instances()
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

    def _purge_orphan_instances(self) -> None:
        """Drop schedule instances whose character no longer exists."""
        from ..services.character_service import CharacterService
        from ..services.schedule_instance_service import ScheduleInstanceService

        char_svc = CharacterService(data_dir=self._data_dir)
        existing = [c.id for c in char_svc.list_characters()]
        removed = ScheduleInstanceService(self._data_dir).delete_orphan_instances(existing)
        if removed:
            logger.warning("ProactiveScheduler: removed %d orphan schedule instance(s)", removed)

    def _recover_startup_locks(self, utc_now: datetime | None = None) -> None:
        from ..services.schedule_instance_service import ScheduleInstanceService

        svc = ScheduleInstanceService(self._data_dir)
        released = svc.release_startup_generation_locks(utc_now=utc_now)
        if released:
            logger.warning("ProactiveScheduler: released %d abandoned generation lock(s)", released)

    async def _process_instance(
        self,
        row: ScheduleInstanceRow,
        svc: ScheduleInstanceService,
        utc_now: datetime,
    ) -> None:
        from ..services.character_service import CharacterNotFoundError, CharacterService

        if not svc.claim_for_generation(row.id, utc_now=utc_now):
            return
        now = utc_now or datetime.now(UTC)
        started = perf_counter()
        registry = get_registry()

        def record(status: str, reason: str) -> None:
            """Append this outcome to the in-memory execution history."""
            registry.record_execution(
                schedule_id=row.id,
                status=status,
                reason=reason,
                character_id=row.character_id,
                conversation_id=row.conversation_id,
                channel=row.channel,
                duration_ms=int((perf_counter() - started) * 1000),
            )

        def fail(reason: str) -> None:
            svc.mark_failed(row.id, reason, utc_now=utc_now)
            record("failed", reason)
            record_error(
                "proactive",
                reason,
                conversation_id=row.conversation_id,
                schedule_id=row.id,
            )

        try:
            char_svc = CharacterService(data_dir=self._data_dir)
            try:
                char = char_svc.get_character(row.character_id)
            except (CharacterNotFoundError, KeyError):
                fail("character not found")
                return

            defn = svc.get_definition_for_row(row) or _get_schedule_definition(char, row.schedule_def_id)
            if defn is None:
                fail("schedule definition not found")
                return
            if not defn.enabled:
                svc.complete_execution(row.id, defn, status="skipped", reason="disabled", utc_now=utc_now)
                record("skipped", "disabled")
                return

            if row.last_sent_at is not None and row.minimum_cooldown_minutes > 0:
                last_sent = row.last_sent_at.replace(tzinfo=UTC)
                if now < last_sent + timedelta(minutes=row.minimum_cooldown_minutes):
                    svc.complete_execution(
                        row.id,
                        defn,
                        status="skipped",
                        reason="cooldown",
                        utc_now=now,
                    )
                    record("skipped", "cooldown")
                    return

            zi = ZoneInfo(row.timezone)
            local_time_str = now.astimezone(zi).strftime("%Y-%m-%d %H:%M ") + row.timezone
            injection = _build_proactive_injection(local_time_str, defn.instruction)

            adapter = make_delivery_adapter(row.channel, self._data_dir)

            message: str | None = None
            if row.decision_mode == "contextual":
                decision = await self._decide_send_or_skip(conversation_id=row.conversation_id, injection=injection)
                if decision.action == "skip":
                    svc.complete_execution(
                        row.id,
                        defn,
                        status="skipped",
                        reason=decision.reason or "contextual_skip",
                        utc_now=now,
                    )
                    record("skipped", decision.reason or "contextual_skip")
                    return
                message = decision.message.strip() or None

            if message is None:
                # Show "typing" only once the decision to send is made, so a
                # contextual skip never flashes a status the user then loses.
                async with adapter.typing(
                    channel_instance_id=row.channel_instance_id,
                    external_chat_id=row.external_chat_id,
                    conversation_id=row.conversation_id,
                ):
                    message = await self._generate(
                        conversation_id=row.conversation_id, proactive_injection=injection
                    )
            if message is None:
                fail("generation_failed")
                return

            self._persist_assistant_message(row.conversation_id, message)
            try:
                await adapter.deliver(
                    channel_instance_id=row.channel_instance_id,
                    external_chat_id=row.external_chat_id,
                    message_text=message,
                    conversation_id=row.conversation_id,
                )
            except Exception:
                logger.exception(
                    "ProactiveScheduler: delivery failed for instance %s (message persisted)",
                    row.id,
                )
                svc.complete_execution(
                    row.id,
                    defn,
                    status="failed",
                    reason="delivery_failed",
                    utc_now=now,
                )
                record("failed", "delivery_failed")
                record_error(
                    "proactive",
                    "delivery failed",
                    bot_id=row.channel_instance_id if row.channel == "telegram" else "",
                    conversation_id=row.conversation_id,
                    schedule_id=row.id,
                )
                return

            svc.complete_execution(row.id, defn, status="sent", utc_now=now, mark_sent=True)
            record("sent", "")
        except Exception as exc:
            logger.exception("ProactiveScheduler: unexpected error for instance %s", row.id)
            svc.mark_failed(row.id, "unexpected_error", utc_now=now)
            record("failed", "unexpected_error")
            record_error(
                "proactive",
                f"unexpected error: {exc}",
                conversation_id=row.conversation_id,
                schedule_id=row.id,
            )

    def _persist_assistant_message(self, conversation_id: str, message: str) -> None:
        from ..services.character_service import CharacterService
        from ..services.conversation_service import ConversationService

        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv_svc.append_message(conversation_id, "assistant", message, images=[])

    async def _decide_send_or_skip(self, *, conversation_id: str, injection: str) -> ProactiveDecision:
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.conversation_service import ConversationService
        from ..services.prompt_service import get_prompt
        from ..services.summary_service import SummaryService

        config = get_config()
        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv = conv_svc.get_conversation(conversation_id)
        char = char_svc.get_character(conv.character_id)
        manager = ConnectorManager(data_dir=self._data_dir, config=config)
        # The send/skip decision is a classification task, not roleplay.
        text_connector = manager.get_text_connector("text_utility")
        if text_connector is None:
            return ProactiveDecision(action="skip", reason="no_active_text_connector")
        summarization_connector = manager.get_text_connector("text_summarization")

        decision_instruction = get_prompt("proactive_decision")
        payload_prompt = f"{injection}\n\n{decision_instruction}"
        stats = self._statistics_service()
        ctx_window, max_tokens = effective_limits(text_connector, config.chat.context_window)
        messages = await SummaryService(self._data_dir).build_prompt_within_budget(
            conv,
            connector=summarization_connector or text_connector,
            context_window=ctx_window,
            threshold=config.chat.summarization_threshold,
            max_tokens=max_tokens,
            statistics_service=stats,
            char=char,
            user_name=config.user.name,
            use_tool_calling=False,
            ooc_guardrail=False,
            proactive_injection=payload_prompt,
        )
        started = perf_counter()
        chunks: list[str] = []
        try:
            async for token in text_connector.stream_chat_completion(messages):
                chunks.append(token)
        except Exception as exc:
            self._record_llm_call(
                stats, conversation_id, text_connector, messages, "",
                started, success=False, error_detail=str(exc),
            )
            record_error("proactive", str(exc), conversation_id=conversation_id)
            raise
        raw = "".join(chunks).strip()
        self._record_llm_call(
            stats, conversation_id, text_connector, messages, raw, started, success=True,
        )
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
        from ..services.conversation_service import ConversationService
        from ..services.summary_service import SummaryService

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
        stats = self._statistics_service()
        started = perf_counter()
        messages: list[dict[str, str]] = []
        ctx_window, max_tokens = effective_limits(text_connector, config.chat.context_window)
        try:
            messages = await SummaryService(data_dir).build_prompt_within_budget(
                conv,
                connector=(
                    connector_manager.get_text_connector("text_summarization")
                    or text_connector
                ),
                context_window=ctx_window,
                threshold=config.chat.summarization_threshold,
                max_tokens=max_tokens,
                statistics_service=stats,
                char=char,
                user_name=config.user.name,
                use_tool_calling=False,
                ooc_guardrail=False,
                proactive_injection=proactive_injection,
            )
            started = perf_counter()
            chunks: list[str] = []
            async for token in text_connector.stream_chat_completion(messages):
                chunks.append(token)
            text = "".join(chunks).strip()
            self._record_llm_call(
                stats, conversation_id, text_connector, messages, text, started, success=True,
            )
            return text or None
        except Exception as exc:
            logger.exception("ProactiveScheduler: generation failed for conversation %s", conversation_id)
            self._record_llm_call(
                stats, conversation_id, text_connector, messages, "",
                started, success=False, error_detail=str(exc),
            )
            record_error("proactive", str(exc), conversation_id=conversation_id)
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


def _get_schedule_definition(char: CharacterCard, schedule_def_id: str) -> ScheduleDefinition | None:
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
