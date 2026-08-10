"""ProactiveScheduler — transport-neutral scheduled message trigger service.

Architecture
------------
The scheduler polls the ``schedule_instances`` table once per minute.
For each row whose ``next_run_at`` is in the past and whose
``generation_started_at`` is NULL (no concurrent generation in progress):

1. **Claim**: set ``generation_started_at = now`` (idempotency lock).
2. **Resolve**: load the character card, find the matching ScheduleDefinition.
3. **Inject**: build a proactive-event system message from the prompt template
   and inject it as the last system message to the chat engine.
4. **Generate**: call the normal AubergeRP chat engine with an empty user turn.
5. **Deliver**: send the generated text to the transport adapter.
6. **Advance**: record ``last_run_at``, recalculate ``next_run_at``, clear lock.

On failure the lock is released so the next tick can retry.

On server startup the scheduler explicitly clears any leftover
``generation_started_at`` locks before the first tick. This allows an
interrupted generation to be retried after a crash/restart instead of staying
stuck forever.

Telegram and Web share the same scheduler; delivery is handled by the
:mod:`delivery_service` transport adapters.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..db_models import ScheduleInstanceRow
from ..services.delivery_service import make_delivery_adapter

if TYPE_CHECKING:
    from ..models.character import CharacterCard, ScheduleDefinition
    from ..services.schedule_instance_service import ScheduleInstanceService

logger = logging.getLogger(__name__)

_PROACTIVE_SYSTEM_PLACEHOLDER = "__PROACTIVE_EVENT__"


def _build_proactive_injection(
    local_time_str: str,
    instruction: str,
) -> str:
    """Render the proactive-event prompt template."""
    from .prompt_service import get_prompt

    template = get_prompt("proactive_event")
    return (
        template
        .replace("{{local_time}}", local_time_str)
        .replace("{{instruction}}", instruction)
    )


class ProactiveScheduler:
    """Background asyncio task that fires scheduled proactive messages."""

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
        # Fire once immediately so overdue schedules are processed on startup
        # without waiting for the first poll interval.
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
        """Process all due schedule instances."""
        from ..services.schedule_instance_service import ScheduleInstanceService

        now = utc_now or datetime.now(UTC)
        svc = ScheduleInstanceService(self._data_dir)
        due_rows = svc.find_due(utc_now=now)

        if due_rows:
            logger.debug("ProactiveScheduler: %d instance(s) due", len(due_rows))

        for row in due_rows:
            await self._process_instance(row, svc, now)

    def _recover_startup_locks(self, utc_now: datetime | None = None) -> None:
        """Release abandoned generation locks from a previous server process."""
        from ..services.schedule_instance_service import ScheduleInstanceService

        svc = ScheduleInstanceService(self._data_dir)
        released = svc.release_startup_generation_locks(utc_now=utc_now)
        if released:
            logger.warning(
                "ProactiveScheduler: released %d abandoned generation lock(s) on startup",
                released,
            )

    async def _process_instance(
        self,
        row: ScheduleInstanceRow,
        svc: ScheduleInstanceService,
        utc_now: datetime,
    ) -> None:
        from ..services.character_service import CharacterNotFoundError, CharacterService

        instance_id = row.id

        # ── 1. Claim ──────────────────────────────────────────────────────────
        if not svc.claim_for_generation(instance_id, utc_now=utc_now):
            # Another process/coroutine already claimed it
            return

        try:
            # ── 2. Resolve character + schedule definition ─────────────────────
            char_svc = CharacterService(data_dir=self._data_dir)
            try:
                char = char_svc.get_character(row.character_id)
            except (CharacterNotFoundError, KeyError):
                logger.warning(
                    "ProactiveScheduler: character %s not found for instance %s; skipping",
                    row.character_id,
                    instance_id,
                )
                svc.release_generation_lock(instance_id)
                return

            defn = _get_schedule_definition(char, row.schedule_def_id)
            if defn is None:
                logger.warning(
                    "ProactiveScheduler: schedule_def '%s' not found in character %s; releasing",
                    row.schedule_def_id,
                    row.character_id,
                )
                svc.release_generation_lock(instance_id)
                return

            if not defn.enabled:
                logger.debug(
                    "ProactiveScheduler: schedule_def '%s' is disabled; skipping",
                    row.schedule_def_id,
                )
                svc.release_generation_lock(instance_id)
                return

            # ── 3. Build proactive injection ───────────────────────────────────
            zi = ZoneInfo(row.timezone)
            local_time_str = utc_now.astimezone(zi).strftime("%Y-%m-%d %H:%M ") + row.timezone
            injection = _build_proactive_injection(local_time_str, defn.instruction)

            # ── 4. Generate via the normal chat engine ─────────────────────────
            result_text = await self._generate(
                conversation_id=row.conversation_id,
                proactive_injection=injection,
            )
            if result_text is None:
                svc.release_generation_lock(instance_id)
                return

            # ── 5. Deliver via transport adapter ──────────────────────────────
            adapter = make_delivery_adapter(row.channel, self._data_dir)
            try:
                await adapter.deliver(
                    channel_instance_id=row.channel_instance_id,
                    external_chat_id=row.external_chat_id,
                    message_text=result_text,
                )
            except Exception:
                logger.exception(
                    "ProactiveScheduler: delivery failed for instance %s (message already persisted)",
                    instance_id,
                )
                # Do not retry generation — message is already in conversation history.

            # ── 6. Advance schedule ────────────────────────────────────────────
            svc.complete_generation(instance_id, defn, utc_now=utc_now)
            logger.info(
                "ProactiveScheduler: fired schedule '%s' for conversation %s (channel=%s)",
                row.schedule_def_id,
                row.conversation_id,
                row.channel,
            )

        except Exception:
            logger.exception(
                "ProactiveScheduler: unexpected error for instance %s; releasing lock",
                instance_id,
            )
            svc.release_generation_lock(instance_id)

    async def _generate(
        self,
        *,
        conversation_id: str,
        proactive_injection: str,
    ) -> str | None:
        """Call the chat engine with a proactive injection system message.

        Returns the generated text, or None on failure.
        The generated assistant message is persisted normally by the engine.
        """
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.chat_service import ChatService, GenerationOptions
        from ..services.conversation_service import ConversationService
        from ..services.media_service import MediaService
        from ..services.statistics_service import StatisticsService

        config = get_config()
        data_dir = self._data_dir
        char_svc = CharacterService(data_dir=data_dir)
        conv_svc = ConversationService(data_dir=data_dir, character_service=char_svc)
        stats_svc = StatisticsService(data_dir=data_dir)
        media_svc = MediaService(data_dir=data_dir)
        connector_manager = ConnectorManager(data_dir=data_dir, config=config)
        images_dir = Path(data_dir) / "images" / "proactive"

        svc = ChatService(
            conversation_service=conv_svc,
            character_service=char_svc,
            connector_manager=connector_manager,
            images_dir=images_dir,
            session_token="proactive",
            context_window=config.chat.context_window,
            summarization_threshold=config.chat.summarization_threshold,
            ooc_protection=False,   # proactive events are generated internally
            statistics_service=stats_svc,
            media_service=media_svc,
            proactive_injection=proactive_injection,
        )

        try:
            result = await svc.generate_reply(
                conversation_id=conversation_id,
                content="",   # No user message — proactive initiation
                options=GenerationOptions(
                    user_name="User",
                    is_proactive=True,
                ),
            )
            return result.text
        except Exception:
            logger.exception(
                "ProactiveScheduler: generation failed for conversation %s",
                conversation_id,
            )
            return None


def _get_schedule_definition(
    char: CharacterCard,
    schedule_def_id: str,
) -> ScheduleDefinition | None:
    """Extract a ScheduleDefinition from a CharacterCard by id."""
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
