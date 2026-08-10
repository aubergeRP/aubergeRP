from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from ..connectors.manager import ConnectorManager
from ..models.character import CharacterCard
from ..models.conversation import Conversation
from ..services.character_service import CharacterService
from ..services.conversation_service import ConversationService, resolve_macros
from ..services.media_service import MediaService
from ..services.prompt_service import get_prompt
from ..services.statistics_service import StatisticsService
from ..services.summarization_service import count_prompt_tokens, maybe_summarize

logger = logging.getLogger(__name__)

_PREFIX = "[IMG:"
_MAX_IMAGE_MARKERS = 3

_IMAGE_PROMPT_TEMPLATE = Path(__file__).parent.parent / "prompts" / "image_prompt.txt"
_IMAGE_PROMPT_MAX_CONTEXT = 6


@dataclass(slots=True)
class GenerationOptions:
    user_name: str = "User"
    retry_deduplicate_user_message: bool = False
    narration_mode: Literal["full", "dialogue_only"] = "full"


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


def build_prompt(
    conversation: Conversation,
    char: CharacterCard,
    user_name: str = "User",
    use_tool_calling: bool = False,
    ooc_guardrail: bool = False,
    nsfw_policy: Literal["none", "block", "allow"] = "none",
    narration_mode: Literal["full", "dialogue_only"] = "full",
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    system_parts: list[str] = []
    base_prompt = char.data.system_prompt if char.data.system_prompt else get_prompt("default_system")
    system_parts.append(resolve_macros(base_prompt, char.data.name, user_name))
    # Append the image instruction appropriate for the backend.
    img_instruction_key = "image_tool_instruction" if use_tool_calling else "image_marker_instruction"
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

    for msg in conversation.messages:
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
        statistics_service: StatisticsService | None = None,
        media_service: MediaService | None = None,
    ) -> None:
        self._conversation_service = conversation_service
        self._character_service = character_service
        self._connector_manager = connector_manager
        self._images_dir = Path(images_dir)
        self._session_token = session_token
        self._context_window = context_window
        self._summarization_threshold = summarization_threshold
        self._ooc_protection = ooc_protection
        self._statistics_service = statistics_service
        self._media_service = media_service

    def _resolve_text_connector_metadata(self, text_connector: Any) -> tuple[str, str, str]:
        connector_id = ""
        connector_name = type(text_connector).__name__
        connector_backend = str(getattr(text_connector, "backend_id", ""))

        get_active = getattr(self._connector_manager, "get_active_id_for_type", None)
        if callable(get_active):
            try:
                active_id = get_active("text")
                if isinstance(active_id, str):
                    connector_id = active_id
            except Exception:
                pass

        if connector_id:
            get_connector = getattr(self._connector_manager, "get_connector", None)
            if callable(get_connector):
                try:
                    instance = get_connector(connector_id)
                    name = getattr(instance, "name", "")
                    backend = getattr(instance, "backend", "")
                    if isinstance(name, str) and name:
                        connector_name = name
                    if isinstance(backend, str) and backend:
                        connector_backend = backend
                except Exception:
                    pass

        return connector_id, connector_name, connector_backend

    def _resolve_active_connector_nsfw(self, connector_type: Literal["text", "image"]) -> bool:
        """Read nsfw flag from the active connector instance config (defaults to False)."""
        get_active = getattr(self._connector_manager, "get_active_id_for_type", None)
        if not callable(get_active):
            return False

        try:
            active_id = get_active(connector_type)
        except Exception:
            return False

        if not isinstance(active_id, str) or not active_id:
            return False

        get_connector = getattr(self._connector_manager, "get_connector", None)
        if not callable(get_connector):
            return False

        try:
            instance = get_connector(active_id)
        except Exception:
            return False

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
        async for event in self._generate_events(conversation_id, content, run_options):
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
        async for event in self._generate_events(conversation_id, content, options):
            yield event

    async def _generate_events(
        self,
        conversation_id: str,
        content: str,
        options: GenerationOptions,
    ) -> AsyncIterator[dict[str, Any]]:
        user_name = options.user_name
        appended_user_message_id: str | None = None
        try:
            conv = self._conversation_service.get_conversation(conversation_id)
            char = self._character_service.get_character(conv.character_id)
        except Exception as exc:
            yield {"type": "error", "detail": str(exc)}
            return

        # Frontend retries should not duplicate the user turn.
        is_retry = (
            options.retry_deduplicate_user_message
            and self._is_retry_message(conv, content)
        )
        if not is_retry:
            try:
                user_message = self._conversation_service.append_message(
                    conversation_id, "user", content
                )
                appended_user_message_id = user_message.id
            except Exception as exc:
                yield {"type": "error", "detail": str(exc)}
                return
            # Reload conversation to include the newly appended user message.
            try:
                conv = self._conversation_service.get_conversation(conversation_id)
            except Exception as exc:
                yield {"type": "error", "detail": str(exc)}
                return

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
        messages = build_prompt(
            conv, char, user_name,
            use_tool_calling=use_tools,
            ooc_guardrail=ooc_detected,
            nsfw_policy=nsfw_policy,
            narration_mode=options.narration_mode,
        )

        # Summarize history if the prompt is approaching the token budget.
        conn_ctx = getattr(getattr(text_connector, "config", None), "context_window", None)
        effective_ctx = conn_ctx if isinstance(conn_ctx, int) and conn_ctx > 0 else self._context_window
        messages = await maybe_summarize(
            messages,
            text_connector,
            effective_ctx,
            self._summarization_threshold,
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
                    text_connector, messages, char
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

                # Extract optional parameters from connector config
                connector_config = getattr(text_connector, "config", None)
                kwargs = {}
                if connector_config:
                    if hasattr(connector_config, "top_p") and connector_config.top_p is not None:
                        kwargs["top_p"] = connector_config.top_p
                    if hasattr(connector_config, "top_k") and connector_config.top_k is not None:
                        kwargs["top_k"] = connector_config.top_k
                    if hasattr(connector_config, "repeat_penalty") and connector_config.repeat_penalty is not None:
                        kwargs["repeat_penalty"] = connector_config.repeat_penalty
                    if hasattr(connector_config, "presence_penalty") and connector_config.presence_penalty is not None:
                        kwargs["presence_penalty"] = connector_config.presence_penalty
                    if hasattr(connector_config, "frequency_penalty") and connector_config.frequency_penalty is not None:
                        kwargs["frequency_penalty"] = connector_config.frequency_penalty
                    if hasattr(connector_config, "extra_body") and connector_config.extra_body:
                        kwargs["extra_body"] = connector_config.extra_body

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
                            async for img_event in self._handle_image(char, gen_id, prompt, text_connector, messages):
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
            }
        finally:
            if self._statistics_service is not None:
                with suppress(Exception):
                    self._statistics_service.record_text_call(
                        conversation_id=conversation_id,
                        connector_id=connector_id,
                        connector_name=connector_name,
                        connector_backend=connector_backend,
                        request_tokens=request_tokens,
                        response_tokens=_estimate_text_tokens(full_text),
                        response_time_ms=int((perf_counter() - call_started) * 1000),
                        success=call_success,
                        error_detail=call_error,
                    )

    async def _stream_with_tools(
        self,
        text_connector: Any,
        messages: list[dict[str, Any]],
        char: CharacterCard,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream using tool calling; handle generate_image tool calls."""
        tools = [_IMAGE_TOOL]

        # Extract optional parameters from connector config
        connector_config = getattr(text_connector, "config", None)
        kwargs = {}
        if connector_config:
            if hasattr(connector_config, "top_p") and connector_config.top_p is not None:
                kwargs["top_p"] = connector_config.top_p
            if hasattr(connector_config, "top_k") and connector_config.top_k is not None:
                kwargs["top_k"] = connector_config.top_k
            if hasattr(connector_config, "repeat_penalty") and connector_config.repeat_penalty is not None:
                kwargs["repeat_penalty"] = connector_config.repeat_penalty
            if hasattr(connector_config, "presence_penalty") and connector_config.presence_penalty is not None:
                kwargs["presence_penalty"] = connector_config.presence_penalty
            if hasattr(connector_config, "frequency_penalty") and connector_config.frequency_penalty is not None:
                kwargs["frequency_penalty"] = connector_config.frequency_penalty
            if hasattr(connector_config, "extra_body") and connector_config.extra_body:
                kwargs["extra_body"] = connector_config.extra_body

        async for event in text_connector.stream_chat_completion_with_tools(messages, tools, **kwargs):
            if event["type"] == "token":
                yield event
            elif event["type"] == "tool_call" and event.get("name") == "generate_image":
                prompt = event.get("arguments", {}).get("prompt", "")
                gen_id = str(uuid.uuid4())
                yield {"type": "image_start", "generation_id": gen_id, "prompt": prompt}
                async for img_event in self._handle_image(char, gen_id, prompt, text_connector, messages):
                    yield img_event

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
    ) -> AsyncIterator[dict[str, Any]]:
        img_connector = self._connector_manager.get_active_image_connector()
        if img_connector is None:
            yield {
                "type": "image_failed",
                "generation_id": gen_id,
                "detail": (
                    "No image connector is configured. "
                    "Please add and activate an image connector in the admin panel."
                ),
            }
            return
        full_prompt = prompt
        try:
            logger.debug("[Image Gen] Starting image generation for gen_id=%s", gen_id)
            if text_connector is not None and messages is not None:
                prompt = await self._generate_image_prompt(
                    text_connector, char, messages, prompt
                )
            if not prompt:
                # Fallback when no text connector or prompt generation failed
                char_desc = (char.data.description or "")[:300]
                prompt = f"{char.data.name}. {char_desc}".strip() if char_desc else char.data.name
            auberge = char.data.extensions.get("aubergeRP", {})
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
                yield {
                    "type": "image_failed",
                    "generation_id": gen_id,
                    "detail": "No image returned",
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
            yield {
                "type": "image_failed",
                "generation_id": gen_id,
                "detail": str(exc),
            }

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
        except Exception:
            logger.error(
                "[Generate Scene Image] Failed to load conversation/character "
                "(conversation_id=%r)",
                conversation_id,
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

        generated_media: list[tuple[str, str]] = []
        async for event in self._handle_image(
            char=char,
            gen_id=gen_id,
            prompt="",
            text_connector=text_connector,
            messages=messages,
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
            yield {
                "type": "image_failed",
                "generation_id": generation_id,
                "detail": str(exc),
            }
            return

        # Re-generate the image with the stored prompt (no LLM enhancement).
        # Pass text_connector=None and messages=None so that _handle_image skips
        # the prompt refinement step and uses the prompt as-is.
        generated_media: list[tuple[str, str]] = []
        async for event in self._handle_image(
            char=char,
            gen_id=generation_id,
            prompt=prompt,
            text_connector=None,
            messages=None,
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
