from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from ..connectors.manager import ConnectorManager
from ..models.character import CharacterCard
from ..models.conversation import Conversation, Message
from ..services.character_service import CharacterService
from ..services.conversation_service import ConversationService, resolve_macros
from ..services.media_service import MediaService
from ..services.observability_service import record_error
from ..services.prompt_service import get_prompt
from ..services.statistics_service import StatisticsService
from ..services.summarization_service import count_prompt_tokens, format_summary_message
from ..services.summary_service import SummaryService

logger = logging.getLogger(__name__)

_PREFIX = "[IMG:"
_MAX_IMAGE_MARKERS = 3

_IMAGE_PROMPT_TEMPLATE = Path(__file__).parent.parent / "prompts" / "image_prompt.txt"
_IMAGE_PROMPT_MAX_CONTEXT = 6

# Shown to end users when image generation fails. The real cause often embeds
# provider URLs and HTTP bodies, so it is recorded in the observability error
# tail (redacted) rather than sent down the SSE stream.
IMAGE_FAILURE_MESSAGE = (
    "Image generation failed. "
    "See Admin → Operations → Recent errors for details."
)

# Automatic retry schedule (seconds) applied when a generation fails before any
# content reached the user. Five attempts in total: the initial one plus four
# spaced retries.
GENERATION_RETRY_DELAYS: tuple[float, ...] = (1.0, 5.0, 20.0, 60.0)


@dataclass(slots=True)
class GenerationOptions:
    user_name: str = "User"
    retry_deduplicate_user_message: bool = False
    narration_mode: Literal["full", "dialogue_only"] = "full"
    is_proactive: bool = False


@dataclass(slots=True)
class GenerationResult:
    text: str
    message_id: str
    images: list[str]


class ChatGenerationError(RuntimeError):
    pass

# ---------------------------------------------------------------------------
# OOC (out-of-character) protection
# ---------------------------------------------------------------------------

_OOC_PATTERNS: list[re.Pattern[str]] = [
    # These patterns cover the most common jailbreak/break-character attempts.
    # They favour low false-negative rate over false-positive rate: a few
    # legitimate roleplay messages may occasionally match (e.g. a character
    # saying "you are an AI in this story"), but the guardrail injection is
    # lightweight (a single system message) so the cost of a false positive
    # is low.
    re.compile(r"\b(ignore (all |your )?(previous )?instructions?)\b", re.IGNORECASE),
    re.compile(
        r"\b(break character|out of character|stop (role)?playing|stop being)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byou are (now )?(an? )?(ai|llm|language model|gpt|chatgpt|claude|assistant)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(pretend (you are|to be) (not |no longer )?)\b", re.IGNORECASE),
    re.compile(r"\b(jailbreak|dan mode|dev mode)\b", re.IGNORECASE),
    re.compile(r"\b(act as (a |an )?(different|new|real|actual))\b", re.IGNORECASE),
]


_NSFW_PATTERNS: list[re.Pattern[str]] = [
    # Lightweight lexical heuristic similar to OOC detection.
    re.compile(r"\b(nsfw|explicit|porn|pornographic|erotic|sexual|sex scene)\b", re.IGNORECASE),
    re.compile(r"\b(nude|nudity|naked|topless|bottomless|full nudity)\b", re.IGNORECASE),
    re.compile(r"\b(fetish|bdsm|domination|submission|kink)\b", re.IGNORECASE),
    # French-language equivalents for multilingual user input detection.
    re.compile(r"\b(contenu sexuel|contenu explicite|nu int\u00e9gral|pornographique)\b", re.IGNORECASE),
]



def detect_ooc(text: str) -> bool:
    """Return True if *text* looks like an out-of-character attempt."""
    return any(p.search(text) for p in _OOC_PATTERNS)


def detect_nsfw(text: str) -> bool:
    """Return True if *text* looks like an NSFW request."""
    return any(p.search(text) for p in _NSFW_PATTERNS)


# ---------------------------------------------------------------------------
# Tool definition for structured image triggers
# ---------------------------------------------------------------------------

_IMAGE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Emit an inline image for the current scene. "
            "Call this ONLY when the user has explicitly requested a visual. "
            "Keep the prompt concrete and under 200 characters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Short English description of the image to generate.",
                }
            },
            "required": ["prompt"],
        },
    },
}

_SCHEDULE_PROACTIVE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "schedule_proactive_message",
        "description": "Create a future proactive trigger for this conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["after_delay", "after_inactivity", "daily_at", "daily_window"],
                },
                "instruction": {"type": "string"},
                "delay_minutes": {"type": "integer"},
                "inactivity_minutes": {"type": "integer"},
                "time": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "not_before_time": {"type": "string"},
                "minimum_cooldown_minutes": {"type": "integer"},
                "one_shot": {"type": "boolean"},
                "enabled": {"type": "boolean"},
            },
            "required": ["type", "instruction"],
        },
    },
}

_CANCEL_SCHEDULE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "cancel_scheduled_message",
        "description": "Cancel a scheduled proactive trigger by schedule id.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
}

_LIST_SCHEDULES_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "list_scheduled_messages",
        "description": "List active proactive schedules in this conversation.",
        "parameters": {"type": "object", "properties": {}},
    },
}

# ---------------------------------------------------------------------------
# [IMG:…] marker parser (fallback for connectors without tool-calling)
# ---------------------------------------------------------------------------


class ImageMarkerParser:
    """State machine that parses [IMG:prompt] markers in streaming text chunks."""

    def __init__(self) -> None:
        self._state = "text"   # "text" | "prefix" | "marker"
        self._buf = ""         # partial prefix buffer
        self._marker_buf = ""  # content inside [IMG:...]
        self._marker_count = 0  # hard cap: max 3 per message

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        pending = ""

        for char in chunk:
            if self._state == "text":
                if char == "[":
                    if pending:
                        events.append({"type": "token", "text": pending})
                        pending = ""
                    self._state = "prefix"
                    self._buf = "["
                else:
                    pending += char

            elif self._state == "prefix":
                candidate = self._buf + char
                if _PREFIX.startswith(candidate):
                    self._buf = candidate
                    if self._buf == _PREFIX:
                        self._state = "marker"
                        self._marker_buf = ""
                        self._buf = ""
                else:
                    events.append({"type": "token", "text": candidate})
                    self._buf = ""
                    self._state = "text"

            elif self._state == "marker":
                if char == "]":
                    if self._marker_count < _MAX_IMAGE_MARKERS:
                        self._marker_count += 1
                        events.append({"type": "image_trigger", "prompt": self._marker_buf})
                    else:
                        # Cap exceeded: emit the marker text as a plain token
                        events.append({"type": "token", "text": _PREFIX + self._marker_buf + "]"})
                    self._marker_buf = ""
                    self._state = "text"
                else:
                    self._marker_buf += char

        if pending:
            events.append({"type": "token", "text": pending})

        return events

    def flush(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self._state == "prefix" and self._buf:
            events.append({"type": "token", "text": self._buf})
            self._buf = ""
        elif self._state == "marker":
            events.append({"type": "token", "text": _PREFIX + self._marker_buf})
            self._marker_buf = ""
        self._state = "text"
        return events




def _split_roleplay_bracket_segments(text: str) -> tuple[list[str], list[str]]:
    """Split user text into dialogue fragments and bracketed instructions."""
    dialogue_parts: list[str] = []
    instructions: list[str] = []

    dialogue_buf: list[str] = []
    instruction_buf: list[str] = []
    opening = ""
    closing = ""

    for char in text:
        if not opening:
            if char in "[{":
                if dialogue_buf:
                    dialogue_parts.append("".join(dialogue_buf))
                    dialogue_buf = []
                opening = char
                closing = "]" if char == "[" else "}"
            else:
                dialogue_buf.append(char)
        else:
            if char == closing:
                segment = "".join(instruction_buf).strip()
                if segment:
                    instructions.append(segment)
                instruction_buf = []
                opening = ""
                closing = ""
            else:
                instruction_buf.append(char)

    if opening:
        dialogue_buf.extend(opening)
        dialogue_buf.extend(instruction_buf)

    if dialogue_buf:
        dialogue_parts.append("".join(dialogue_buf))

    return dialogue_parts, instructions


def _format_user_message_for_llm(content: str) -> str:
    """Format user message so the LLM can distinguish dialogue and directions."""
    dialogue_parts, instructions = _split_roleplay_bracket_segments(content)
    if not instructions:
        return content

    dialogue = " ".join(part.strip() for part in dialogue_parts if part.strip())
    blocks: list[str] = []
    if dialogue:
        blocks.append(f"Dialogue:\n{dialogue}")

    instruction_lines = "\n".join(f"- {instruction}" for instruction in instructions)
    blocks.append(f"Roleplay instructions (non-dialogue):\n{instruction_lines}")
    return "\n\n".join(blocks)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _connector_model(text_connector: Any) -> str:
    """Return the model name configured on *text_connector*, if any."""
    return str(getattr(getattr(text_connector, "config", None), "model", "") or "")


# Optional sampling parameters forwarded from the connector config to the
# completion call. `extra_body` is falsy-checked (an empty dict means "unset"),
# the others only need to be non-None.
_SAMPLING_PARAMS = (
    "top_p",
    "top_k",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
)


def _sampling_kwargs(text_connector: Any) -> dict[str, Any]:
    """Collect the optional sampling parameters set on the connector config."""
    config = getattr(text_connector, "config", None)
    if not config:
        return {}
    kwargs: dict[str, Any] = {}
    for name in _SAMPLING_PARAMS:
        value = getattr(config, name, None)
        if value is not None:
            kwargs[name] = value
    extra_body = getattr(config, "extra_body", None)
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def autonomy_allowed(conversation: Conversation, cooldown: int) -> bool:
    """Return False if an image was produced in the last *cooldown* assistant messages.

    Cheap, stateless anti-spam guard: the autonomous image instruction is only
    injected once the character has gone `cooldown` replies without a picture.
    """
    if cooldown <= 0:
        return True
    seen = 0
    for msg in reversed(conversation.messages):
        if msg.role != "assistant":
            continue
        if msg.images:
            return False
        seen += 1
        if seen >= cooldown:
            break
    return True


def build_prompt(
    conversation: Conversation,
    char: CharacterCard,
    user_name: str = "User",
    use_tool_calling: bool = False,
    ooc_guardrail: bool = False,
    nsfw_policy: Literal["none", "block", "allow"] = "none",
    narration_mode: Literal["full", "dialogue_only"] = "full",
    proactive_injection: str | None = None,
    image_enabled: bool = True,
    image_autonomy: bool = False,
    history: list[Message] | None = None,
    summary_text: str | None = None,
) -> list[dict[str, str]]:
    """Build the chat prompt.

    *history* overrides ``conversation.messages`` — callers pass the messages
    that came after the persisted summary.  *summary_text* is that summary; it
    is inserted right after the system block so the model keeps the earlier
    narrative even though those messages are no longer sent.
    """
    messages: list[dict[str, str]] = []

    system_parts: list[str] = []
    base_prompt = char.data.system_prompt if char.data.system_prompt else get_prompt("default_system")
    system_parts.append(resolve_macros(base_prompt, char.data.name, user_name))
    # Append the image instruction appropriate for the backend — but only when an
    # image connector is actually available, otherwise the model would emit
    # markers/tool calls that can never produce anything.
    if image_enabled:
        img_instruction_key = (
            "image_tool_instruction" if use_tool_calling else "image_marker_instruction"
        )
        if image_autonomy:
            img_instruction_key += "_autonomous"
        system_parts.append(get_prompt(img_instruction_key))
    system_parts.append(get_prompt("roleplay_bracket_instruction"))
    no_reasoning = get_prompt("no_reasoning_instruction")
    if no_reasoning:
        system_parts.append(no_reasoning)
    if char.data.description:
        system_parts.append(
            f"{char.data.name}'s description: "
            f"{resolve_macros(char.data.description, char.data.name, user_name)}"
        )
    if char.data.personality:
        system_parts.append(
            f"{char.data.name}'s personality: "
            f"{resolve_macros(char.data.personality, char.data.name, user_name)}"
        )
    if char.data.scenario:
        system_parts.append(
            f"Scenario: {resolve_macros(char.data.scenario, char.data.name, user_name)}"
        )
    if char.data.mes_example:
        system_parts.append(
            f"Example dialogue:\n"
            f"{resolve_macros(char.data.mes_example, char.data.name, user_name)}"
        )
    if ooc_guardrail:
        system_parts.append(get_prompt("ooc_guardrail"))
    if nsfw_policy == "block":
        system_parts.append(get_prompt("nsfw_block_guardrail"))
    elif nsfw_policy == "allow":
        system_parts.append(get_prompt("nsfw_allow_guardrail"))
    messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    if summary_text:
        messages.append({"role": "system", "content": format_summary_message(summary_text)})

    for msg in (conversation.messages if history is None else history):
        content = msg.content
        if msg.role == "user":
            content = _format_user_message_for_llm(content)
        messages.append({"role": msg.role, "content": content})

    if char.data.post_history_instructions:
        messages.append({
            "role": "system",
            "content": resolve_macros(
                char.data.post_history_instructions, char.data.name, user_name
            ),
        })

    if narration_mode == "dialogue_only":
        dialogue_only_instruction = get_prompt("dialogue_only_instruction")
        if dialogue_only_instruction:
            messages.append({"role": "system", "content": dialogue_only_instruction})

    if proactive_injection:
        messages.append({"role": "system", "content": proactive_injection})

    return messages


class ChatService:
    def __init__(
        self,
        conversation_service: ConversationService,
        character_service: CharacterService,
        connector_manager: ConnectorManager,
        images_dir: Path | str,
        session_token: str = "",
        context_window: int = 4096,
        summarization_threshold: float = 0.75,
        ooc_protection: bool = True,
        image_autonomy: bool = False,
        image_autonomy_cooldown: int = 4,
        statistics_service: StatisticsService | None = None,
        media_service: MediaService | None = None,
        proactive_injection: str | None = None,
        channel: str = "web",
        channel_instance_id: str = "web",
        external_user_id: str = "",
        external_chat_id: str = "",
        generation_type: str = "chat",
    ) -> None:
        self._conversation_service = conversation_service
        self._character_service = character_service
        self._connector_manager = connector_manager
        self._images_dir = Path(images_dir)
        self._session_token = session_token
        self._context_window = context_window
        self._summarization_threshold = summarization_threshold
        self._ooc_protection = ooc_protection
        self._image_autonomy = image_autonomy
        self._image_autonomy_cooldown = image_autonomy_cooldown
        self._statistics_service = statistics_service
        self._media_service = media_service
        self._proactive_injection = proactive_injection
        self._channel = channel
        self._channel_instance_id = channel_instance_id
        self._external_user_id = external_user_id
        self._external_chat_id = external_chat_id
        self._generation_type = generation_type
        self._summary_service = SummaryService(conversation_service.data_dir)

    def _resolve_active_connector(
        self, connector_type: Literal["text", "image"]
    ) -> tuple[str, Any]:
        """Return the active connector ``(id, instance)`` for *connector_type*.

        Both parts degrade independently: the id is ``""`` when nothing is
        active, and the instance is ``None`` when it cannot be looked up. The
        connector manager is duck-typed because tests substitute stubs for it.
        """
        get_active = getattr(self._connector_manager, "get_active_id_for_type", None)
        if not callable(get_active):
            return "", None
        try:
            active_id = get_active(connector_type)
        except Exception:
            return "", None
        if not isinstance(active_id, str) or not active_id:
            return "", None

        get_connector = getattr(self._connector_manager, "get_connector", None)
        if not callable(get_connector):
            return active_id, None
        try:
            return active_id, get_connector(active_id)
        except Exception:
            return active_id, None

    def _role_connector(self, role: str, fallback: Any) -> Any:
        """Return the text connector for *role*, degrading to *fallback*.

        The manager is duck-typed because tests substitute stubs for it.
        """
        get_for_role = getattr(self._connector_manager, "get_text_connector", None)
        if not callable(get_for_role):
            return fallback
        try:
            conn = get_for_role(role)
        except Exception:
            logger.warning("Failed to resolve '%s' connector", role, exc_info=True)
            return fallback
        return conn if conn is not None else fallback

    def _resolve_text_connector_metadata(self, text_connector: Any) -> tuple[str, str, str]:
        connector_id, instance = self._resolve_active_connector("text")
        connector_name = type(text_connector).__name__
        connector_backend = str(getattr(text_connector, "backend_id", ""))

        name = getattr(instance, "name", "")
        backend = getattr(instance, "backend", "")
        if isinstance(name, str) and name:
            connector_name = name
        if isinstance(backend, str) and backend:
            connector_backend = backend

        return connector_id, connector_name, connector_backend

    def _resolve_active_connector_nsfw(self, connector_type: Literal["text", "image"]) -> bool:
        """Read nsfw flag from the active connector instance config (defaults to False)."""
        _, instance = self._resolve_active_connector(connector_type)
        config = getattr(instance, "config", {})
        if not isinstance(config, dict):
            return False
        return bool(config.get("nsfw", False))

    @staticmethod
    def _is_retry_message(conversation: Conversation, content: str) -> bool:
        last_msg = conversation.messages[-1] if conversation.messages else None
        return bool(
            last_msg is not None
            and last_msg.role == "user"
            and last_msg.content == content
        )

    def _rollback_user_message(self, conversation_id: str, message_id: str | None) -> None:
        if message_id is None:
            return
        try:
            self._conversation_service.delete_message(conversation_id, message_id)
        except Exception:
            logger.warning(
                "Failed to roll back user message %s for conversation %s",
                message_id,
                conversation_id,
                exc_info=True,
            )

    async def generate_reply(
        self,
        conversation_id: str,
        content: str,
        options: GenerationOptions | None = None,
    ) -> GenerationResult:
        run_options = options or GenerationOptions()
        done_event: dict[str, Any] | None = None
        async for event in self._generate_events_with_retry(
            conversation_id, content, run_options
        ):
            if event["type"] == "error":
                raise ChatGenerationError(str(event.get("detail", "Chat generation failed")))
            if event["type"] == "done":
                done_event = event
        if done_event is None:
            raise ChatGenerationError("Chat generation did not complete")
        return GenerationResult(
            text=str(done_event.get("full_content", "")),
            message_id=str(done_event.get("message_id", "")),
            images=list(done_event.get("images", [])),
        )

    async def stream_chat(
        self,
        conversation_id: str,
        content: str,
        user_name: str = "User",
    ) -> AsyncIterator[dict[str, Any]]:
        options = GenerationOptions(
            user_name=user_name,
            retry_deduplicate_user_message=True,
        )
        async for event in self._generate_events_with_retry(
            conversation_id, content, options
        ):
            yield event

    async def _generate_events_with_retry(
        self,
        conversation_id: str,
        content: str,
        options: GenerationOptions,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a generation, retrying with backoff when it fails silently.

        A retry is only attempted while nothing has been emitted to the caller
        yet: once tokens or images have been streamed, the partial reply is the
        user-visible state and restarting would duplicate it.
        """
        delays = GENERATION_RETRY_DELAYS
        for attempt in range(len(delays) + 1):
            is_last = attempt == len(delays)
            emitted_content = False
            failed = False
            async with aclosing(
                self._generate_events(conversation_id, content, options)
            ) as events:
                async for event in events:
                    if (
                        event["type"] == "error"
                        and event.get("retryable")
                        and not emitted_content
                        and not is_last
                    ):
                        failed = True
                        break
                    if event["type"] in ("token", "image_start", "image_complete"):
                        emitted_content = True
                    yield event
            if not failed:
                return
            delay = delays[attempt]
            logger.warning(
                "Chat generation failed for conversation %s, retrying in %.0fs "
                "(attempt %d/%d)",
                conversation_id,
                delay,
                attempt + 1,
                len(delays) + 1,
            )
            # Doubles as an SSE keep-alive while waiting; unknown to the UI,
            # which simply ignores it.
            yield {"type": "retry", "attempt": attempt + 1, "delay": delay}
            await asyncio.sleep(delay)

    async def _generate_events(
        self,
        conversation_id: str,
        content: str,
        options: GenerationOptions,
    ) -> AsyncGenerator[dict[str, Any], None]:
        user_name = options.user_name
        appended_user_message_id: str | None = None
        try:
            conv = self._conversation_service.get_conversation(conversation_id)
            char = self._character_service.get_character(conv.character_id)
        except Exception:
            logger.warning("ChatService failed to load conversation context", exc_info=True)
            yield {"type": "error", "detail": "Unable to load conversation context."}
            return

        # Frontend retries should not duplicate the user turn.
        # Proactive events have no user turn to add.
        is_retry = (
            options.retry_deduplicate_user_message
            and self._is_retry_message(conv, content)
        )
        if not is_retry and not options.is_proactive:
            try:
                user_message = self._conversation_service.append_message(
                    conversation_id, "user", content
                )
                appended_user_message_id = user_message.id
            except Exception:
                logger.warning("ChatService failed to append user message", exc_info=True)
                yield {"type": "error", "detail": "Unable to persist user message."}
                return
            # Reload conversation to include the newly appended user message.
            try:
                conv = self._conversation_service.get_conversation(conversation_id)
            except Exception:
                logger.warning("ChatService failed to reload conversation", exc_info=True)
                yield {"type": "error", "detail": "Unable to reload conversation."}
                return
            with suppress(Exception):
                from ..services.schedule_instance_service import ScheduleInstanceService

                ScheduleInstanceService(self._conversation_service.data_dir).rebase_event_triggers_on_user_message(
                    conversation_id,
                    user_message_at=user_message.timestamp,
                )

        text_connector = self._connector_manager.get_active_text_connector()
        if text_connector is None:
            self._rollback_user_message(conversation_id, appended_user_message_id)
            yield {"type": "error", "detail": "No active text connector configured"}
            return

        # OOC detection: if the user message looks like a break-character attempt,
        # inject a guardrail into the system prompt.
        ooc_detected = self._ooc_protection and detect_ooc(content)

        # NSFW detection follows the same pattern as OOC: detect from user input,
        # then inject a targeted guardrail based on the active text connector policy.
        nsfw_detected = detect_nsfw(content)
        text_nsfw_enabled = self._resolve_active_connector_nsfw("text")
        nsfw_policy: Literal["none", "block", "allow"] = "none"
        if nsfw_detected:
            nsfw_policy = "allow" if text_nsfw_enabled else "block"

        use_tools = getattr(text_connector, "supports_tool_calling", False)
        image_enabled = self._connector_manager.get_active_image_connector() is not None
        image_autonomy = (
            image_enabled
            and self._image_autonomy
            and autonomy_allowed(conv, self._image_autonomy_cooldown)
        )
        # The prompt is built from the stored summary plus the messages that
        # followed it; a new summary is produced only when the budget is hit.
        conn_ctx = getattr(getattr(text_connector, "config", None), "context_window", None)
        effective_ctx = conn_ctx if isinstance(conn_ctx, int) and conn_ctx > 0 else self._context_window
        messages = await self._summary_service.build_prompt_within_budget(
            conv,
            connector=self._role_connector("text_summarization", text_connector),
            context_window=effective_ctx,
            threshold=self._summarization_threshold,
            statistics_service=self._statistics_service,
            char=char,
            user_name=user_name,
            use_tool_calling=use_tools,
            image_enabled=image_enabled,
            image_autonomy=image_autonomy,
            ooc_guardrail=ooc_detected,
            nsfw_policy=nsfw_policy,
            narration_mode=options.narration_mode,
            proactive_injection=self._proactive_injection,
        )

        full_text = ""
        image_urls: list[str] = []
        image_prompts_by_generation: dict[str, str] = {}
        generated_media: list[tuple[str, str]] = []
        assistant_persisted = False
        request_tokens = count_prompt_tokens(messages)
        call_started = perf_counter()
        call_success = False
        call_error = ""
        connector_id, connector_name, connector_backend = self._resolve_text_connector_metadata(
            text_connector
        )

        try:
            if use_tools:
                async for event in self._stream_with_tools(
                    text_connector, messages, char, conversation_id
                ):
                    if event["type"] == "token":
                        full_text += event["content"]
                        yield {"type": "token", "content": event["content"]}
                    elif event["type"] == "image_start":
                        gen_id = str(event.get("generation_id", ""))
                        prompt = str(event.get("prompt", ""))
                        if gen_id:
                            image_prompts_by_generation[gen_id] = prompt
                        yield event
                    elif event["type"] == "image_complete":
                        image_urls.append(event["image_url"])
                        gen_id = str(event.get("generation_id", ""))
                        generated_media.append(
                            (event["image_url"], image_prompts_by_generation.get(gen_id, ""))
                        )
                        yield event
                    else:
                        yield event
            else:
                parser = ImageMarkerParser()

                kwargs = _sampling_kwargs(text_connector)
                async for chunk in text_connector.stream_chat_completion(messages, **kwargs):
                    for ev in parser.feed(chunk):
                        if ev["type"] == "token":
                            full_text += ev["text"]
                            yield {"type": "token", "content": ev["text"]}
                        elif ev["type"] == "image_trigger":
                            gen_id = str(uuid.uuid4())
                            prompt = ev["prompt"]
                            yield {
                                "type": "image_start",
                                "generation_id": gen_id,
                                "prompt": prompt,
                            }
                            image_prompts_by_generation[gen_id] = prompt
                            async for img_event in self._handle_image(
                                char, gen_id, prompt, text_connector, messages,
                                conversation_id=conversation_id,
                            ):
                                if img_event["type"] == "image_complete":
                                    image_urls.append(img_event["image_url"])
                                    generated_media.append(
                                        (
                                            img_event["image_url"],
                                            image_prompts_by_generation.get(gen_id, ""),
                                        )
                                    )
                                yield img_event

                for ev in parser.flush():
                    if ev["type"] == "token":
                        full_text += ev["text"]
                        yield {"type": "token", "content": ev["text"]}

            msg = self._conversation_service.append_message(
                conversation_id, "assistant", full_text, images=image_urls
            )
            assistant_persisted = True
            if self._media_service is not None and generated_media:
                self._media_service.record_generated_media(
                    conversation_id=conversation_id,
                    message_id=msg.id,
                    media_items=generated_media,
                )
            call_success = True
            if not full_text and not image_urls:
                logger.warning(
                    "LLM returned an empty response for conversation %s. "
                    "If you are using a reasoning model, consider: "
                    "1) checking that the no_reasoning_instruction system prompt is effective, "
                    "2) raising the max_tokens limit to accommodate reasoning output.",
                    conversation_id,
                )
                yield {
                    "type": "warning",
                    "detail": (
                        "The model returned an empty response. "
                        "If you are using a reasoning model (e.g. DeepSeek-R1, Qwen3), "
                        "its thinking may have consumed all available tokens. "
                        "Try raising the max_tokens limit in the connector settings, "
                        "or update the system prompt to discourage lengthy reasoning."
                    ),
                }
            yield {
                "type": "done",
                "message_id": msg.id,
                "full_content": full_text,
                "images": image_urls,
            }

        except Exception as exc:
            call_error = str(exc)
            record_error("llm", call_error, conversation_id=conversation_id)
            if not assistant_persisted:
                self._rollback_user_message(conversation_id, appended_user_message_id)
            logger.exception(
                "Chat generation failed for conversation %s", conversation_id
            )
            yield {
                "type": "error",
                "detail": (
                    "An error occurred while generating a response. "
                    "Please check the server logs for details."
                ),
                # Provider/network failures are worth retrying; configuration
                # errors yielded earlier are not.
                "retryable": True,
            }
        finally:
            if self._statistics_service is not None:
                with suppress(Exception):
                    # Prefer the provider's own usage report; fall back to the
                    # local ~4-chars-per-token heuristic when it is absent.
                    usage = getattr(text_connector, "last_usage", None)
                    if isinstance(usage, dict):
                        tokens_in = int(usage.get("prompt_tokens", 0))
                        tokens_out = int(usage.get("completion_tokens", 0))
                        estimated = False
                    else:
                        tokens_in = request_tokens
                        tokens_out = _estimate_text_tokens(full_text)
                        estimated = True
                    self._statistics_service.record_text_call(
                        conversation_id=conversation_id,
                        connector_id=connector_id,
                        connector_name=connector_name,
                        connector_backend=connector_backend,
                        request_tokens=tokens_in,
                        response_tokens=tokens_out,
                        response_time_ms=int((perf_counter() - call_started) * 1000),
                        success=call_success,
                        error_detail=call_error,
                        generation_type=self._generation_type,
                        model=_connector_model(text_connector),
                        tokens_estimated=estimated,
                    )

    async def _stream_with_tools(
        self,
        text_connector: Any,
        messages: list[dict[str, Any]],
        char: CharacterCard,
        conversation_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream using tool calling; handle generate_image tool calls."""
        tools = [_IMAGE_TOOL, _SCHEDULE_PROACTIVE_TOOL, _CANCEL_SCHEDULE_TOOL, _LIST_SCHEDULES_TOOL]

        kwargs = _sampling_kwargs(text_connector)
        async for event in text_connector.stream_chat_completion_with_tools(messages, tools, **kwargs):
            if event["type"] == "token":
                yield event
            elif event["type"] == "tool_call" and event.get("name") == "generate_image":
                prompt = event.get("arguments", {}).get("prompt", "")
                gen_id = str(uuid.uuid4())
                yield {"type": "image_start", "generation_id": gen_id, "prompt": prompt}
                async for img_event in self._handle_image(
                    char, gen_id, prompt, text_connector, messages,
                    conversation_id=conversation_id,
                ):
                    yield img_event
            elif event["type"] == "tool_call":
                try:
                    result = await self._handle_proactive_tool_call(
                        conversation_id=conversation_id,
                        character_id=char.id,
                        tool_name=str(event.get("name", "")),
                        arguments=event.get("arguments", {}) or {},
                    )
                    if result is not None:
                        yield {"type": "tool_result", "name": str(event.get("name", "")), "result": result}
                except Exception as exc:
                    logger.warning("Proactive tool call failed: %s", exc)
                    yield {"type": "warning", "detail": "A proactive tool call failed."}

    async def _handle_proactive_tool_call(
        self,
        *,
        conversation_id: str,
        character_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        from ..models.character import ProactiveConfig, ScheduleDefinition
        from ..services.schedule_instance_service import ScheduleInstanceService
        from ..services.timezone_service import TimezoneService

        sched_svc = ScheduleInstanceService(self._conversation_service.data_dir)
        if tool_name == "schedule_proactive_message":
            ext = self._character_service.get_character(character_id).data.extensions.get("aubergerp", {})
            proactive = ProactiveConfig(**(ext.get("proactive", {}) if isinstance(ext, dict) else {}))
            defn = ScheduleDefinition(
                id=str(arguments.get("id") or f"tool_{uuid.uuid4().hex[:12]}"),
                enabled=bool(arguments.get("enabled", True)),
                type=str(arguments.get("type", "after_delay")),  # type: ignore[arg-type]
                time=arguments.get("time"),
                start=arguments.get("start"),
                end=arguments.get("end"),
                delay_minutes=arguments.get("delay_minutes"),
                inactivity_minutes=arguments.get("inactivity_minutes"),
                not_before_time=arguments.get("not_before_time"),
                minimum_cooldown_minutes=arguments.get("minimum_cooldown_minutes"),
                one_shot=bool(arguments.get("one_shot", False)),
                instruction=str(arguments.get("instruction", "")).strip(),
            )
            sched_svc.get_or_create(
                defn=defn,
                character_id=character_id,
                conversation_id=conversation_id,
                channel=self._channel,
                channel_instance_id=self._channel_instance_id,
                external_user_id=self._external_user_id or self._session_token or "web-user",
                external_chat_id=self._external_chat_id,
                timezone=TimezoneService(self._conversation_service.data_dir).get_timezone_name(
                    self._channel,
                    self._channel_instance_id,
                    self._external_user_id or self._session_token or "web-user",
                )
                or "UTC",
                origin="character-tool",
                decision_mode=proactive.decision_mode,
                proactive=proactive,
            )
            return {"status": "scheduled", "id": defn.id}

        if tool_name == "cancel_scheduled_message":
            schedule_id = str(arguments.get("id", "")).strip()
            if not schedule_id:
                return {"status": "ignored", "reason": "missing id"}
            rows = sched_svc.list_for_conversation(conversation_id)
            deleted = 0
            for row in rows:
                if row.schedule_def_id == schedule_id:
                    sched_svc.delete_instance(row.id)
                    deleted += 1
            return {"status": "cancelled", "id": schedule_id, "deleted": deleted}

        if tool_name == "list_scheduled_messages":
            rows = sched_svc.list_for_conversation(conversation_id)
            return {
                "status": "ok",
                "count": len(rows),
                "items": [
                    {
                        "id": r.schedule_def_id,
                        "origin": r.origin,
                        "trigger_type": r.trigger_type,
                        "enabled": r.enabled,
                        "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
                    }
                    for r in rows
                ],
            }
        return None

    async def _generate_image_prompt(
        self,
        text_connector: Any,
        char: CharacterCard,
        messages: list[dict[str, Any]],
        raw_prompt: str,
    ) -> str:
        """Use the LLM to build a detailed image generation prompt from scene context.

        Falls back to *raw_prompt* on any error so image generation is never blocked.
        """
        try:
            template = _IMAGE_PROMPT_TEMPLATE.read_text(encoding="utf-8")
            convo_msgs = [m for m in messages if m.get("role") != "system"]
            recent = convo_msgs[-_IMAGE_PROMPT_MAX_CONTEXT:]
            recent_exchanges = "\n".join(
                f"{m['role'].capitalize()}: {str(m.get('content', ''))[:400]}"
                for m in recent
            ) or "(no prior exchanges)"
            char_desc = (char.data.description or "")[:600]
            char_scenario = (
                f"Scenario: {char.data.scenario[:400]}" if char.data.scenario else ""
            )
            user_content = template.format(
                char_name=char.data.name,
                char_description=char_desc,
                char_scenario=char_scenario,
                recent_exchanges=recent_exchanges,
                raw_keywords=raw_prompt or "(none)",
            )
            tokens: list[str] = []
            async for chunk in text_connector.stream_chat_completion(
                [{"role": "user", "content": user_content}],
                max_tokens=2048,
                temperature=0.7,
            ):
                tokens.append(chunk)
            result = "".join(tokens).strip()
            return result if result else raw_prompt
        except Exception:
            return raw_prompt

    async def _handle_image(
        self,
        char: CharacterCard,
        gen_id: str,
        prompt: str,
        text_connector: Any | None = None,
        messages: list[dict[str, Any]] | None = None,
        conversation_id: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        img_connector = self._connector_manager.get_active_image_connector()
        if img_connector is None:
            # Actionable and secret-free, so this one is shown verbatim.
            detail = (
                "No image connector is configured. "
                "Please add and activate an image connector in the admin panel."
            )
            record_error("image", detail, conversation_id=conversation_id)
            yield {
                "type": "image_failed",
                "generation_id": gen_id,
                "detail": detail,
            }
            return
        full_prompt = prompt
        try:
            logger.debug("[Image Gen] Starting image generation for gen_id=%s", gen_id)
            if text_connector is not None and messages is not None:
                # Building the image prompt is a utility task: it may run on a
                # different (cheaper) model than the roleplay reply.
                prompt = await self._generate_image_prompt(
                    self._role_connector("text_utility", text_connector),
                    char, messages, prompt,
                )
            if not prompt:
                # Fallback when no text connector or prompt generation failed
                char_desc = (char.data.description or "")[:300]
                prompt = f"{char.data.name}. {char_desc}".strip() if char_desc else char.data.name
            auberge = char.data.extensions.get("aubergerp", {})
            prefix = auberge.get("image_prompt_prefix", "")
            negative = auberge.get("negative_prompt", "")
            full_prompt = f"{prefix} {prompt}".strip() if prefix else prompt
            logger.debug(
                "[Image Gen] Full prompt: %s... (len=%d)", full_prompt[:200], len(full_prompt)
            )
            img_bytes: bytes | None = None
            async for event in img_connector.generate_image_with_progress(
                full_prompt, negative_prompt=negative
            ):
                if event["type"] == "progress":
                    yield {
                        "type": "image_progress",
                        "generation_id": gen_id,
                        "step": event["step"],
                        "total": event["total"],
                    }
                elif event["type"] == "complete":
                    img_bytes = event["bytes"]
            if img_bytes is None:
                record_error(
                    "image",
                    "no image returned by connector",
                    conversation_id=conversation_id,
                )
                yield {
                    "type": "image_failed",
                    "generation_id": gen_id,
                    "detail": IMAGE_FAILURE_MESSAGE,
                }
                return
            self._images_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid.uuid4()}.png"
            (self._images_dir / filename).write_bytes(img_bytes)
            url = f"/api/images/{self._session_token}/{filename}"
            logger.debug(
                "[Image Gen] Successfully generated image (gen_id=%s, size=%d bytes)",
                gen_id,
                len(img_bytes),
            )
            yield {"type": "image_complete", "generation_id": gen_id, "image_url": url, "prompt": full_prompt}
        except Exception as exc:
            logger.exception(
                "[Image Gen] Error generating image (gen_id=%s, prompt=%r)",
                gen_id,
                full_prompt[:200],
            )
            record_error("image", str(exc), conversation_id=conversation_id)
            yield {
                "type": "image_failed",
                "generation_id": gen_id,
                "detail": IMAGE_FAILURE_MESSAGE,
            }

    async def _stream_image_and_record(
        self,
        *,
        char: CharacterCard,
        gen_id: str,
        prompt: str,
        conversation_id: str,
        text_connector: Any | None,
        messages: list[dict[str, Any]] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a standalone (message-less) image generation and log the result."""
        generated_media: list[tuple[str, str]] = []
        async for event in self._handle_image(
            char=char,
            gen_id=gen_id,
            prompt=prompt,
            text_connector=text_connector,
            messages=messages,
            conversation_id=conversation_id,
        ):
            if event["type"] == "image_complete":
                generated_media.append((event["image_url"], event.get("prompt", "")))
            yield event

        if self._media_service is not None and generated_media:
            self._media_service.record_generated_media(
                conversation_id=conversation_id,
                message_id="",
                media_items=generated_media,
            )

    async def generate_scene_image(
        self,
        conversation_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Generate an image of the current scene from the conversation context.

        Uses the text connector (if available) to build a rich image prompt from
        the recent conversation history, then delegates to the active image connector.
        """
        try:
            conv = self._conversation_service.get_conversation(conversation_id)
            char = self._character_service.get_character(conv.character_id)
        except Exception as exc:
            logger.error(
                "[Generate Scene Image] Failed to load conversation/character "
                "(conversation_id=%r)",
                conversation_id,
            )
            record_error(
                "image",
                f"failed to load conversation/character: {exc}",
                conversation_id=conversation_id,
            )
            gen_id = str(uuid.uuid4())
            yield {
                "type": "image_failed",
                "generation_id": gen_id,
                "detail": "Failed to load conversation",
            }
            return

        gen_id = str(uuid.uuid4())
        yield {"type": "image_start", "generation_id": gen_id, "prompt": ""}

        text_connector = self._connector_manager.get_active_text_connector()
        messages: list[dict[str, Any]] | None = None
        if text_connector is not None:
            try:
                messages = build_prompt(conv, char)
            except Exception:
                messages = None

        async for event in self._stream_image_and_record(
            char=char,
            gen_id=gen_id,
            prompt="",
            conversation_id=conversation_id,
            text_connector=text_connector,
            messages=messages,
        ):
            yield event

    async def retry_generate_image(
        self,
        conversation_id: str,
        prompt: str,
        generation_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Retry generation of a single image with the given prompt and generation_id.

        This is called when a user clicks the "Retry" button after an image
        generation failure. The prompt is re-used without modification or
        LLM-based enhancement — it is treated as the final, user-approved prompt.
        """
        try:
            conv = self._conversation_service.get_conversation(conversation_id)
            char = self._character_service.get_character(conv.character_id)
        except Exception as exc:
            logger.error(f"[Retry Image] Failed to load conversation/character: {exc}")
            record_error(
                "image",
                f"failed to load conversation/character: {exc}",
                conversation_id=conversation_id,
            )
            yield {
                "type": "image_failed",
                "generation_id": generation_id,
                "detail": IMAGE_FAILURE_MESSAGE,
            }
            return

        # Re-generate the image with the stored prompt (no LLM enhancement).
        # Pass text_connector=None and messages=None so that _handle_image skips
        # the prompt refinement step and uses the prompt as-is.
        async for event in self._stream_image_and_record(
            char=char,
            gen_id=generation_id,
            prompt=prompt,
            conversation_id=conversation_id,
            text_connector=None,
            messages=None,
        ):
            yield event
