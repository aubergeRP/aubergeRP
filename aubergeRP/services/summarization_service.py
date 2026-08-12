"""Conversation summarization primitives.

When the messages that would be sent to the LLM approach a configurable
fraction of the model's context window, the oldest non-system messages are
compressed into a single summary.  Summaries are *persisted* and reused —
see :mod:`aubergeRP.services.summary_service` for the stateful side; this
module holds only the pure helpers (token accounting and the LLM round-trip).

Token counting uses a simple four-characters-per-token heuristic so that no
extra dependency (tiktoken etc.) is required.
"""
from __future__ import annotations

import logging
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

# Marker prefixing the summary system message inside a prompt.
SUMMARY_MARKER = "[Summary of earlier conversation]"


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


def prompt_budget(context_window: int, threshold: float) -> int:
    """Return the token budget a prompt must stay under before summarizing."""
    return int(context_window * threshold) - _REPLY_RESERVE_TOKENS


def format_summary_message(summary_text: str) -> str:
    """Return the system-message content carrying *summary_text*."""
    return f"{SUMMARY_MARKER}\n{summary_text.strip()}"


def _build_summary_prompt(
    messages: list[dict[str, Any]],
    previous_summary: str = "",
) -> list[dict[str, Any]]:
    """Construct a prompt that asks the LLM to summarize a conversation excerpt.

    *previous_summary* — when set — is prepended to the excerpt so the new
    summary extends the previous one instead of losing everything before it.
    """
    excerpt_lines: list[str] = []
    if previous_summary:
        excerpt_lines.append(f"{SUMMARY_MARKER}\n{previous_summary.strip()}")
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


async def summarize_excerpt(
    excerpt: list[dict[str, Any]],
    connector: TextConnector,
    *,
    previous_summary: str = "",
    conversation_id: str = "",
    statistics_service: StatisticsService | None = None,
) -> str | None:
    """Summarize *excerpt* with the LLM, returning ``None`` when it fails.

    The call is non-streaming (chunks are collected) and always recorded in
    the statistics under ``generation_type="summarization"``.
    """
    summary_prompt = _build_summary_prompt(excerpt, previous_summary)
    summary_text = ""
    started = perf_counter()
    try:
        async for chunk in connector.stream_chat_completion(summary_prompt):
            summary_text += chunk
    except Exception as exc:
        logger.exception(
            "Summarization failed for conversation %s", conversation_id or "(unknown)"
        )
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
        return None

    _record_summarization_call(
        statistics_service,
        conversation_id=conversation_id,
        connector=connector,
        prompt=summary_prompt,
        summary_text=summary_text,
        started=started,
        success=True,
    )
    summary_text = summary_text.strip()
    return summary_text or None


def summarized_content_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """Return the summary content if one of the messages is a summary marker."""
    for msg in messages:
        if msg.get("role") == "system" and str(msg.get("content", "")).startswith(
            SUMMARY_MARKER
        ):
            content = msg.get("content")
            return content if isinstance(content, str) else str(content)
    return None
