"""Automatic conversation summarization.

When the messages that would be sent to the LLM approach a configurable
fraction of the model's context window the oldest non-system messages are
summarized into a single system message.  This keeps the prompt within budget
without losing the narrative thread.

Token counting uses a simple four-characters-per-token heuristic so that no
extra dependency (tiktoken etc.) is required.
"""
from __future__ import annotations

import logging
from datetime import UTC
from time import perf_counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..connectors.base import TextConnector
    from ..services.statistics_service import StatisticsService

from ..services.observability_service import record_error
from ..services.prompt_service import get_prompt

logger = logging.getLogger(__name__)

# Each message carries a small fixed overhead beyond its content.
_MSG_OVERHEAD_TOKENS = 4
# Reserve some tokens for the new user turn and the assistant's reply.
_REPLY_RESERVE_TOKENS = 256
# Keep at least this many recent messages intact even after summarization.
_MIN_RECENT_MESSAGES = 4


def _count_tokens(text: str) -> int:
    """Approximate token count: ~4 characters per token.

    This heuristic is intentionally model-agnostic and avoids external
    dependencies.  It may be less accurate for non-English text or
    code-heavy content; when in doubt, use a lower summarization_threshold.
    """
    return max(1, len(text) // 4)


def _count_message_tokens(msg: dict[str, Any]) -> int:
    return _count_tokens(msg.get("content") or "") + _MSG_OVERHEAD_TOKENS


def count_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Return an approximate total token count for a list of chat messages."""
    return sum(_count_message_tokens(m) for m in messages)


def _build_summary_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Construct a prompt that asks the LLM to summarize a conversation excerpt."""
    excerpt_lines: list[str] = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content") or ""
        excerpt_lines.append(f"{role.upper()}: {content}")
    excerpt = "\n\n".join(excerpt_lines)
    user_template = get_prompt("summarization_user")
    return [
        {"role": "system", "content": get_prompt("summarization_system")},
        {"role": "user", "content": user_template.format(excerpt=excerpt)},
    ]


def _record_summarization_call(
    statistics_service: StatisticsService | None,
    *,
    conversation_id: str,
    connector: TextConnector,
    prompt: list[dict[str, Any]],
    summary_text: str,
    started: float,
    success: bool,
    error_detail: str = "",
) -> None:
    """Record the summarization LLM call in ``llm_call_stats``.

    Summarization used to be invisible in the statistics even though it is a
    full LLM round-trip.  Recording it under its own ``generation_type`` keeps
    the accounting honest without storing any prompt or summary text.
    """
    if statistics_service is None or not conversation_id:
        return
    usage = getattr(connector, "last_usage", None)
    if isinstance(usage, dict):
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        estimated = False
    else:
        tokens_in = count_prompt_tokens(prompt)
        tokens_out = _count_tokens(summary_text) if summary_text else 0
        estimated = True
    try:
        statistics_service.record_text_call(
            conversation_id=conversation_id,
            connector_id="",
            connector_name=type(connector).__name__,
            connector_backend=str(getattr(connector, "backend_id", "")),
            request_tokens=tokens_in,
            response_tokens=tokens_out,
            response_time_ms=int((perf_counter() - started) * 1000),
            success=success,
            error_detail=error_detail,
            generation_type="summarization",
            model=str(getattr(getattr(connector, "config", None), "model", "") or ""),
            tokens_estimated=estimated,
        )
    except Exception:  # pragma: no cover - statistics must never break chat
        logger.debug("Failed to record summarization statistics", exc_info=True)


async def maybe_summarize(
    messages: list[dict[str, Any]],
    connector: TextConnector,
    context_window: int,
    threshold: float,
    *,
    conversation_id: str = "",
    statistics_service: StatisticsService | None = None,
) -> list[dict[str, Any]]:
    """Return *messages* (possibly with older turns replaced by a summary).

    If the estimated token count is below *threshold* × *context_window* the
    list is returned unchanged.  Otherwise the oldest non-system messages (all
    but the *_MIN_RECENT_MESSAGES* most recent) are summarised into a single
    system message that is inserted right after the initial system block.
    """
    budget = int(context_window * threshold) - _REPLY_RESERVE_TOKENS
    if count_prompt_tokens(messages) <= budget:
        return messages

    # Split into system-header, candidates-to-summarize, and tail-to-keep.
    # The leading block of system messages is always preserved verbatim.
    system_head: list[dict[str, Any]] = []
    remainder: list[dict[str, Any]] = []
    in_head = True
    for msg in messages:
        if in_head and msg.get("role") == "system":
            system_head.append(msg)
        else:
            in_head = False
            remainder.append(msg)

    # Keep the most recent messages intact.
    cutoff = max(0, len(remainder) - _MIN_RECENT_MESSAGES)
    if cutoff == 0:
        # Nothing to summarize — return as-is to avoid infinite calls.
        return messages

    to_summarize = remainder[:cutoff]
    to_keep = remainder[cutoff:]

    # Call the LLM to produce a summary (non-streaming, collected).
    summary_text = ""
    summary_prompt = _build_summary_prompt(to_summarize)
    started = perf_counter()
    try:
        async for chunk in connector.stream_chat_completion(summary_prompt):
            summary_text += chunk
    except Exception as exc:
        # If the summarization call fails, fall back to the original messages.
        logger.exception("Summarization failed for conversation %s", conversation_id or "(unknown)")
        record_error("summarization", str(exc), conversation_id=conversation_id)
        _record_summarization_call(
            statistics_service,
            conversation_id=conversation_id,
            connector=connector,
            prompt=summary_prompt,
            summary_text="",
            started=started,
            success=False,
            error_detail=str(exc),
        )
        return messages

    _record_summarization_call(
        statistics_service,
        conversation_id=conversation_id,
        connector=connector,
        prompt=summary_prompt,
        summary_text=summary_text,
        started=started,
        success=True,
    )

    summary_msg: dict[str, Any] = {
        "role": "system",
        "content": f"[Summary of earlier conversation]\n{summary_text.strip()}",
    }
    return [*system_head, summary_msg, *to_keep]


def summarized_content_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """Return the summary content if the first non-system message is a summary marker."""
    for msg in messages:
        if msg.get("role") == "system" and str(msg.get("content", "")).startswith(
            "[Summary of earlier conversation]"
        ):
            content = msg.get("content")
            return content if isinstance(content, str) else str(content)
    return None


def pack_summary_into_conversation(
    conversation_messages: list[Any],
    summary_text: str,
    kept_count: int,
) -> list[Any]:
    """Replace the oldest messages in the stored conversation with a summary.

    *conversation_messages* is the list of :class:`Message` model objects.
    *kept_count* is the number of recent messages to preserve.
    Returns a new list where the summarized messages are replaced by a single
    summary message.
    """
    # This helper is intentionally thin — callers supply the Message factory.
    # It returns a plain dict so the caller can wrap it in Message as needed.
    import uuid
    from datetime import datetime

    cutoff = max(0, len(conversation_messages) - kept_count)
    kept = conversation_messages[cutoff:]
    now = datetime.now(UTC)
    summary_entry = {
        "id": str(uuid.uuid4()),
        "role": "system",
        "content": f"[Summary of earlier conversation]\n{summary_text}",
        "images": [],
        "timestamp": now.isoformat(),
    }
    return [summary_entry] + [m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in kept]


def to_json_safe(obj: Any) -> Any:
    """Minimal JSON serialisation helper for datetime objects."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
