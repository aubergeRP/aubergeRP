"""Operational observability for aubergeRP.

This module has two responsibilities:

1.  **Process-local runtime state** — a handful of bounded, in-memory
    registries for things nothing else records: recent operational errors,
    recent proactive executions and per-Telegram-bot runtime counters.
    Nothing here is persisted; the buffers are bounded and are lost on
    restart.  Durable history is Prometheus' job (see ``/metrics``).

2.  **Aggregation** — :class:`ObservabilityService` reads the existing tables
    (``llm_call_stats``, ``conversations``, ``messages``, ``channel_sessions``,
    ``telegram_bots``, ``schedule_instances``) and the registries above and
    produces the payloads consumed by both the admin dashboard and
    ``/metrics``.  No new statistics are stored anywhere.

Every free-text string that enters a registry or leaves the API is passed
through :func:`redact` so that tokens, API keys and webhook secrets can never
surface in the dashboard.
"""
from __future__ import annotations

import contextlib
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from ..db_models import (
    ChannelSessionRow,
    CharacterRow,
    ConversationRow,
    ConversationSummaryRow,
    LLMCallStatRow,
    MessageRow,
    ScheduleInstanceRow,
    TelegramBotRow,
    UserTimezoneRow,
)

# Ring buffer sizes.  Deliberately small — this is an operational tail, not a
# log store.
MAX_ERRORS = 200
MAX_EXECUTIONS = 200


#: Instant the process started; the basis for the uptime reading.
PROCESS_STARTED_AT = datetime.now(UTC)

ERROR_COMPONENTS = (
    "llm",
    "image",
    "summarization",
    "telegram_polling",
    "telegram_webhook",
    "telegram_delivery",
    "scheduler",
    "proactive",
    "background",
)

GENERATION_TYPES = ("chat", "proactive", "summarization", "image_prompt")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_MAX_SUMMARY_LEN = 500

# Telegram bot tokens look like "123456789:AA...".
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Provider API keys.
_API_KEY_RE = re.compile(r"\b(?:sk|xoxb|ghp|hf)-[A-Za-z0-9_-]{8,}\b")
# Authorization headers, with or without a scheme.
_AUTH_HEADER_RE = re.compile(r"(?i)\bauthorization\b\s*[:=][^\r\n]*")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+\S+")
# Secret-bearing query parameters.
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|secret|api_?key|access_token|password)=)[^&\s]+"
)

_REDACTED = "[REDACTED]"

# Exact secret values registered at runtime (bot tokens, connector API keys,
# webhook secrets).  Guarded by a lock because bots start concurrently.
_known_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Register *value* so it is scrubbed from any observability output.

    Short values are ignored — redacting them would mangle unrelated text.
    """
    if value and len(value) >= 8:
        with _secrets_lock:
            _known_secrets.add(value)


def reset_secrets() -> None:
    """Clear the registered secrets (used by tests)."""
    with _secrets_lock:
        _known_secrets.clear()


def redact(text: str | None) -> str:
    """Return *text* with credentials removed and length bounded."""
    if not text:
        return ""
    result = str(text)
    with _secrets_lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in result:
            result = result.replace(secret, _REDACTED)
    result = _TELEGRAM_TOKEN_RE.sub(_REDACTED, result)
    result = _API_KEY_RE.sub(_REDACTED, result)
    # Header first: it swallows the whole value, including a "Bearer <token>".
    result = _AUTH_HEADER_RE.sub(f"Authorization: {_REDACTED}", result)
    result = _BEARER_RE.sub(f"Bearer {_REDACTED}", result)
    result = _QUERY_SECRET_RE.sub(rf"\1{_REDACTED}", result)
    if len(result) > _MAX_SUMMARY_LEN:
        result = result[:_MAX_SUMMARY_LEN] + "…"
    return result


def mask_identifier(value: str | None) -> str:
    """Return a short, non-reversible-enough display form of an external user id.

    External user identifiers belong to third parties; the dashboard only needs
    enough to correlate rows, not the full value.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= 4:
        return "…" + text
    return "…" + text[-4:]


# ---------------------------------------------------------------------------
# Runtime registries
# ---------------------------------------------------------------------------

@dataclass
class OperationalError:
    """One recent operational failure, already redacted."""

    timestamp: datetime
    component: str
    summary: str
    bot_id: str = ""
    conversation_id: str = ""
    schedule_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "component": self.component,
            "summary": self.summary,
            "bot_id": self.bot_id,
            "conversation_id": self.conversation_id,
            "schedule_id": self.schedule_id,
        }


@dataclass
class ExecutionRecord:
    """One proactive schedule execution outcome."""

    timestamp: datetime
    schedule_id: str
    character_id: str = ""
    conversation_id: str = ""
    channel: str = ""
    status: str = ""           # sent | skipped | failed
    reason: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "schedule_id": self.schedule_id,
            "character_id": self.character_id,
            "conversation_id": self.conversation_id,
            "channel": self.channel,
            "status": self.status,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
        }


@dataclass
class TelegramRuntimeStats:
    """In-memory runtime counters for one Telegram bot."""

    started_at: datetime | None = None
    stopped_at: datetime | None = None
    mode: str = ""
    last_update_at: datetime | None = None
    last_message_sent_at: datetime | None = None
    updates_received: int = 0
    messages_sent: int = 0
    delivery_failures: int = 0
    last_runtime_error: str = ""
    last_runtime_error_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "started_at": iso(self.started_at),
            "stopped_at": iso(self.stopped_at),
            "mode": self.mode,
            "last_update_at": iso(self.last_update_at),
            "last_message_sent_at": iso(self.last_message_sent_at),
            "updates_received": self.updates_received,
            "messages_sent": self.messages_sent,
            "delivery_failures": self.delivery_failures,
            "last_runtime_error": self.last_runtime_error,
            "last_runtime_error_at": iso(self.last_runtime_error_at),
        }


class RuntimeRegistry:
    """Bounded, process-local operational state.

    A single module-level instance is shared by every component; access is
    guarded by a lock because Telegram bots, the proactive scheduler and the
    request handlers all write to it from the same event loop but potentially
    from different threads (``TestClient``, background tasks).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._errors: deque[OperationalError] = deque(maxlen=MAX_ERRORS)
        self._executions: deque[ExecutionRecord] = deque(maxlen=MAX_EXECUTIONS)
        self._telegram: dict[str, TelegramRuntimeStats] = {}

    # ── errors ───────────────────────────────────────────────────────────

    def record_error(
        self,
        component: str,
        summary: str,
        *,
        bot_id: str = "",
        conversation_id: str = "",
        schedule_id: str = "",
    ) -> None:
        entry = OperationalError(
            timestamp=datetime.now(UTC),
            component=component,
            summary=redact(summary),
            bot_id=bot_id,
            conversation_id=conversation_id,
            schedule_id=schedule_id,
        )
        with self._lock:
            self._errors.append(entry)

    def list_errors(
        self,
        *,
        component: str = "",
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[OperationalError]:
        with self._lock:
            items = list(self._errors)
        items.reverse()  # newest first
        if component:
            items = [e for e in items if e.component == component]
        if since is not None:
            items = [e for e in items if e.timestamp >= since]
        return items[: max(1, limit)]

    def error_counts(self) -> dict[str, int]:
        with self._lock:
            items = list(self._errors)
        counts: dict[str, int] = {c: 0 for c in ERROR_COMPONENTS}
        for entry in items:
            counts[entry.component] = counts.get(entry.component, 0) + 1
        return counts

    # ── proactive executions ─────────────────────────────────────────────

    def record_execution(
        self,
        *,
        schedule_id: str,
        status: str,
        reason: str = "",
        character_id: str = "",
        conversation_id: str = "",
        channel: str = "",
        duration_ms: int = 0,
    ) -> None:
        entry = ExecutionRecord(
            timestamp=datetime.now(UTC),
            schedule_id=schedule_id,
            character_id=character_id,
            conversation_id=conversation_id,
            channel=channel,
            status=status,
            reason=redact(reason),
            duration_ms=max(0, duration_ms),
        )
        with self._lock:
            self._executions.append(entry)

    def list_executions(
        self,
        *,
        schedule_id: str = "",
        status: str = "",
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ExecutionRecord]:
        with self._lock:
            items = list(self._executions)
        items.reverse()
        if schedule_id:
            items = [e for e in items if e.schedule_id == schedule_id]
        if status:
            items = [e for e in items if e.status == status]
        if since is not None:
            items = [e for e in items if e.timestamp >= since]
        return items[: max(1, limit)]

    def execution_counts(self) -> dict[str, int]:
        with self._lock:
            items = list(self._executions)
        counts = {"sent": 0, "skipped": 0, "failed": 0}
        for entry in items:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        return counts

    # ── telegram runtime ─────────────────────────────────────────────────

    def telegram(self, bot_id: str) -> TelegramRuntimeStats:
        with self._lock:
            stats = self._telegram.get(bot_id)
            if stats is None:
                stats = TelegramRuntimeStats()
                self._telegram[bot_id] = stats
            return stats

    def telegram_snapshot(self, bot_id: str) -> TelegramRuntimeStats | None:
        with self._lock:
            return self._telegram.get(bot_id)

    def mark_bot_started(self, bot_id: str, mode: str) -> None:
        stats = self.telegram(bot_id)
        stats.started_at = datetime.now(UTC)
        stats.stopped_at = None
        stats.mode = mode
        stats.last_runtime_error = ""
        stats.last_runtime_error_at = None

    def mark_bot_stopped(self, bot_id: str) -> None:
        stats = self.telegram(bot_id)
        stats.stopped_at = datetime.now(UTC)

    def mark_bot_error(self, bot_id: str, detail: str) -> None:
        stats = self.telegram(bot_id)
        stats.last_runtime_error = redact(detail)
        stats.last_runtime_error_at = datetime.now(UTC)

    def mark_update_received(self, bot_id: str) -> None:
        stats = self.telegram(bot_id)
        stats.last_update_at = datetime.now(UTC)
        stats.updates_received += 1

    def mark_message_sent(self, bot_id: str) -> None:
        stats = self.telegram(bot_id)
        stats.last_message_sent_at = datetime.now(UTC)
        stats.messages_sent += 1

    def mark_delivery_failure(self, bot_id: str) -> None:
        stats = self.telegram(bot_id)
        stats.delivery_failures += 1

    # ── tests ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            self._errors.clear()
            self._executions.clear()
            self._telegram.clear()


_registry = RuntimeRegistry()


def get_registry() -> RuntimeRegistry:
    """Return the process-wide runtime registry."""
    return _registry


def record_error(
    component: str,
    summary: str,
    *,
    bot_id: str = "",
    conversation_id: str = "",
    schedule_id: str = "",
) -> None:
    """Module-level shortcut for :meth:`RuntimeRegistry.record_error`."""
    _registry.record_error(
        component,
        summary,
        bot_id=bot_id,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
    )


def uptime_seconds() -> float:
    """Seconds since the process started."""
    return (datetime.now(UTC) - PROCESS_STARTED_AT).total_seconds()


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _iso(value: datetime | None) -> str | None:
    normalized = _ensure_utc(value)
    return normalized.isoformat() if normalized else None


@dataclass
class LLMFilters:
    """Query filters shared by the LLM-facing views."""

    hours: int = 24
    generation_type: str = ""
    conversation_id: str = ""
    success: bool | None = None
    limit: int = 50
    since: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.since = datetime.now(UTC) - timedelta(hours=max(1, self.hours))


class ObservabilityService:
    """Read-only aggregation over the existing tables + runtime registry."""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        from ..database import init_db

        init_db(self._data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine

        return Session(get_engine(self._data_dir))

    # ── overview ─────────────────────────────────────────────────────────

    def get_overview(self, *, hours: int = 24) -> dict[str, Any]:
        from .. import __version__

        since = datetime.now(UTC) - timedelta(hours=max(1, hours))
        db_path = self._data_dir / "auberge.db"
        db_ok = True
        conversations = messages = sessions = 0
        active_conversations = 0
        try:
            with self._get_session() as session:
                conversations = len(list(session.exec(select(ConversationRow.id)).all()))
                sessions = len(list(session.exec(select(ChannelSessionRow.id)).all()))
                message_rows = list(session.exec(select(MessageRow.conversation_id, MessageRow.timestamp)).all())
        except Exception as exc:  # pragma: no cover - defensive
            db_ok = False
            message_rows = []
            record_error("background", f"database read failed: {exc}")

        messages = len(message_rows)
        active_conversations = len({
            conv_id
            for conv_id, ts in message_rows
            if (_ensure_utc(ts) or PROCESS_STARTED_AT) >= since
        })

        llm = self.get_llm_summary(hours=hours)
        telegram = self.get_telegram_bots()
        schedules = self.get_schedule_summary()
        memory = self.get_memory_summary(hours=hours)

        return {
            "system": {
                "version": __version__,
                "started_at": PROCESS_STARTED_AT.isoformat(),
                "uptime_seconds": round(uptime_seconds(), 1),
                "database_ok": db_ok,
                "database_path": str(db_path),
                "database_size_bytes": db_path.stat().st_size if db_path.exists() else 0,
                "conversations": conversations,
                "messages": messages,
                "sessions": sessions,
                "active_conversations": active_conversations,
            },
            "telegram": {
                "configured": len(telegram),
                "enabled": sum(1 for b in telegram if b["enabled"]),
                "running": sum(1 for b in telegram if b["runtime_state"] == "running"),
                "stopped": sum(1 for b in telegram if b["runtime_state"] == "stopped"),
                "error": sum(1 for b in telegram if b["runtime_state"] == "error"),
                "polling": sum(1 for b in telegram if b["update_mode"] == "polling"),
                "webhook": sum(1 for b in telegram if b["update_mode"] == "webhook"),
                "delivery_failures": sum(b["delivery_failures"] for b in telegram),
            },
            "llm": llm,
            "proactive": schedules,
            "memory": memory,
            "errors": {
                "recent": len(self.get_errors(hours=hours, limit=MAX_ERRORS)),
                "by_component": _registry.error_counts(),
            },
            "range_hours": max(1, hours),
            "generated_at": datetime.now(UTC).isoformat(),
        }

    # ── telegram ─────────────────────────────────────────────────────────

    def get_telegram_bots(self) -> list[dict[str, Any]]:
        """Per-bot configuration + runtime state.  Never returns secrets."""
        from ..routers.telegram import _get_manager

        manager = _get_manager()
        with self._get_session() as session:
            bots = list(session.exec(select(TelegramBotRow)).all())
            characters = {
                row.id: row for row in session.exec(select(CharacterRow)).all()
            }
            session_counts: dict[str, int] = {}
            for row in session.exec(select(ChannelSessionRow)).all():
                if row.channel == "telegram":
                    session_counts[row.channel_instance_id] = (
                        session_counts.get(row.channel_instance_id, 0) + 1
                    )

        result: list[dict[str, Any]] = []
        for bot in sorted(bots, key=lambda b: b.name.lower()):
            register_secret(bot.token)
            register_secret(bot.webhook_secret)
            stats = _registry.telegram_snapshot(bot.id) or TelegramRuntimeStats()
            running = bool(manager and manager.is_running(bot.id))
            if running:
                runtime_state = "running"
            elif bot.enabled:
                # Enabled but no live task means it crashed or never started.
                runtime_state = "error"
            else:
                runtime_state = "stopped"

            character = characters.get(bot.character_id)
            character_name = ""
            if character is not None:
                character_name = str(character.get_data().get("name") or "")

            result.append({
                "id": bot.id,
                "name": bot.name,
                "username": bot.telegram_username,
                "character_id": bot.character_id,
                "character_name": character_name,
                "enabled": bot.enabled,
                "runtime_state": runtime_state,
                "update_mode": bot.update_mode,
                "webhook_configured": bool(bot.webhook_url),
                "sessions": session_counts.get(bot.id, 0),
                "last_tested_at": _iso(bot.last_tested_at),
                "last_error": redact(bot.last_error),
                "webhook_last_error": redact(bot.webhook_last_error),
                **stats.to_dict(),
            })
        return result

    async def get_webhook_info(self, bot_id: str) -> dict[str, Any]:
        """Query Telegram for the live webhook state of *bot_id*.

        The webhook URL is returned as reported by Telegram but scrubbed of any
        secret query parameters; the secret token itself is never exposed.
        """
        from .telegram_bot_service import TelegramBotService

        svc = TelegramBotService(self._data_dir)
        bot_row = svc.get_bot(bot_id)  # raises KeyError when unknown
        if bot_row.update_mode != "webhook":
            return {"bot_id": bot_id, "available": False, "detail": "bot is not in webhook mode"}

        try:
            from aiogram import Bot
        except ImportError:
            return {"bot_id": bot_id, "available": False, "detail": "aiogram is not installed"}

        token = svc.get_bot_token(bot_id)
        register_secret(token)
        bot = Bot(token=token)
        try:
            info = await bot.get_webhook_info()
            return {
                "bot_id": bot_id,
                "available": True,
                "url": redact(getattr(info, "url", "") or ""),
                "pending_update_count": int(getattr(info, "pending_update_count", 0) or 0),
                "last_error_message": redact(getattr(info, "last_error_message", "") or ""),
                "last_error_date": _iso(getattr(info, "last_error_date", None)),
                "max_connections": getattr(info, "max_connections", None),
                "ip_address": getattr(info, "ip_address", None),
            }
        except Exception as exc:
            record_error("telegram_webhook", f"get_webhook_info failed: {exc}", bot_id=bot_id)
            return {"bot_id": bot_id, "available": False, "detail": redact(str(exc))}
        finally:
            with contextlib.suppress(Exception):  # pragma: no cover - best effort
                await bot.session.close()

    # ── sessions ─────────────────────────────────────────────────────────

    def get_sessions(
        self,
        *,
        transport: str = "",
        bot_id: str = "",
        character_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._get_session() as session:
            sessions = list(session.exec(select(ChannelSessionRow)).all())
            conversations = {c.id: c for c in session.exec(select(ConversationRow)).all()}
            timezones = {
                (t.channel, t.channel_instance_id, t.external_user_id): t.timezone
                for t in session.exec(select(UserTimezoneRow)).all()
            }
            bot_names = {b.id: b.name for b in session.exec(select(TelegramBotRow)).all()}
            messages = list(session.exec(select(MessageRow)).all())

        per_conversation: dict[str, dict[str, Any]] = {}
        for msg in messages:
            entry = per_conversation.setdefault(
                msg.conversation_id,
                {"count": 0, "last_user": None, "last_assistant": None},
            )
            entry["count"] += 1
            ts = _ensure_utc(msg.timestamp)
            if msg.role == "user" and (entry["last_user"] is None or ts > entry["last_user"]):
                entry["last_user"] = ts
            elif msg.role == "assistant" and (
                entry["last_assistant"] is None or ts > entry["last_assistant"]
            ):
                entry["last_assistant"] = ts

        rows: list[dict[str, Any]] = []
        for row in sessions:
            conversation = conversations.get(row.conversation_id)
            if transport and row.channel != transport:
                continue
            if bot_id and row.channel_instance_id != bot_id:
                continue
            if character_id and (conversation is None or conversation.character_id != character_id):
                continue
            counters = per_conversation.get(row.conversation_id, {})
            channel_name = bot_names.get(row.channel_instance_id, row.channel_instance_id)
            rows.append({
                "id": row.id,
                "transport": row.channel,
                "channel_instance_id": row.channel_instance_id,
                "channel_name": channel_name,
                "character_id": conversation.character_id if conversation else "",
                "character_name": conversation.character_name if conversation else "",
                "conversation_id": row.conversation_id,
                "conversation_title": conversation.title if conversation else "",
                "user_ref": mask_identifier(row.external_user_id),
                "timezone": timezones.get(
                    (row.channel, row.channel_instance_id, row.external_user_id), ""
                ),
                "message_count": counters.get("count", 0),
                "last_user_activity": _iso(counters.get("last_user")),
                "last_assistant_activity": _iso(counters.get("last_assistant")),
                "updated_at": _iso(row.updated_at),
            })

        rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
        return rows[: max(1, limit)]

    # ── LLM ──────────────────────────────────────────────────────────────

    def _llm_rows(self, filters: LLMFilters) -> list[LLMCallStatRow]:
        with self._get_session() as session:
            rows = list(session.exec(select(LLMCallStatRow)).all())
        selected: list[LLMCallStatRow] = []
        for row in rows:
            created = _ensure_utc(row.created_at)
            if created is None or created < filters.since:
                continue
            if filters.generation_type and row.generation_type != filters.generation_type:
                continue
            if filters.conversation_id and row.conversation_id != filters.conversation_id:
                continue
            if filters.success is not None and row.success != filters.success:
                continue
            selected.append(row)
        selected.sort(key=lambda r: _ensure_utc(r.created_at) or PROCESS_STARTED_AT, reverse=True)
        return selected

    @staticmethod
    def _aggregate(rows: list[LLMCallStatRow]) -> dict[str, Any]:
        total = len(rows)
        failed = sum(1 for r in rows if not r.success)
        latency = sum(max(0, r.response_time_ms) for r in rows)
        tokens_in = sum(max(0, r.request_tokens) for r in rows)
        tokens_out = sum(max(0, r.response_tokens) for r in rows)
        estimated = any(r.tokens_estimated for r in rows)
        return {
            "generations": total,
            "succeeded": total - failed,
            "failed": failed,
            "failure_rate": round((failed / total) * 100.0, 1) if total else 0.0,
            "avg_latency_ms": round(latency / total, 1) if total else 0.0,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "tokens_estimated": estimated,
        }

    def _summarize(self, rows: list[LLMCallStatRow]) -> dict[str, Any]:
        by_type: dict[str, dict[str, Any]] = {}
        for gen_type in GENERATION_TYPES:
            subset = [r for r in rows if r.generation_type == gen_type]
            if subset:
                by_type[gen_type] = self._aggregate(subset)
        summary = self._aggregate(rows)
        summary["by_type"] = by_type
        return summary

    def get_llm_summary(self, *, hours: int = 24) -> dict[str, Any]:
        """Unfiltered aggregate over *hours*, used by the overview and metrics."""
        return self._summarize(self._llm_rows(LLMFilters(hours=hours)))

    def get_llm(
        self,
        *,
        hours: int = 24,
        generation_type: str = "",
        conversation_id: str = "",
        success: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        filters = LLMFilters(
            hours=hours,
            generation_type=generation_type,
            conversation_id=conversation_id,
            success=success,
            limit=limit,
        )
        rows = self._llm_rows(filters)
        with self._get_session() as session:
            titles = {c.id: c.title for c in session.exec(select(ConversationRow)).all()}

        recent = [
            {
                "id": row.id,
                "timestamp": _iso(row.created_at),
                "conversation_id": row.conversation_id,
                "conversation_title": titles.get(row.conversation_id, ""),
                "generation_type": row.generation_type,
                "connector_name": row.connector_name,
                "connector_backend": row.connector_backend,
                "model": row.model,
                "duration_ms": row.response_time_ms,
                "success": row.success,
                "tokens_in": row.request_tokens,
                "tokens_out": row.response_tokens,
                "tokens_estimated": row.tokens_estimated,
                "error_detail": redact(row.error_detail),
            }
            for row in rows[: max(1, limit)]
        ]
        # The summary reflects the same filters as the listing below it.
        summary = self._summarize(rows)
        failures = [
            entry for entry in
            (
                {
                    "id": row.id,
                    "timestamp": _iso(row.created_at),
                    "conversation_id": row.conversation_id,
                    "generation_type": row.generation_type,
                    "connector_name": row.connector_name,
                    "error_detail": redact(row.error_detail),
                }
                for row in rows if not row.success
            )
        ][: max(1, limit)]
        return {
            "summary": summary,
            "recent": recent,
            "failures": failures,
            "range_hours": max(1, hours),
        }

    # ── memory / context ─────────────────────────────────────────────────

    def get_memory_summary(self, *, hours: int = 24) -> dict[str, Any]:
        rows = self._llm_rows(LLMFilters(hours=hours, generation_type="summarization"))
        succeeded = sum(1 for r in rows if r.success)
        return {
            "summaries_generated": succeeded,
            "summarization_failures": len(rows) - succeeded,
        }

    def get_memory(self, *, limit: int = 50, conversation_id: str = "") -> dict[str, Any]:
        from ..config import get_config
        from .summarization_service import count_prompt_tokens

        config = get_config()
        context_limit = config.chat.context_window
        threshold = config.chat.summarization_threshold

        with self._get_session() as session:
            conversations = list(session.exec(select(ConversationRow)).all())
            messages = list(session.exec(select(MessageRow)).all())
            stat_rows = [
                r for r in session.exec(select(LLMCallStatRow)).all()
                if r.generation_type == "summarization"
            ]
            summary_rows = list(session.exec(select(ConversationSummaryRow)).all())

        stored_summaries = {row.conversation_id for row in summary_rows}

        by_conversation: dict[str, list[MessageRow]] = {}
        for msg in messages:
            by_conversation.setdefault(msg.conversation_id, []).append(msg)

        last_summary: dict[str, LLMCallStatRow] = {}
        failure_counts: dict[str, int] = {}
        for row in sorted(stat_rows, key=lambda r: _ensure_utc(r.created_at) or PROCESS_STARTED_AT):
            if row.success:
                last_summary[row.conversation_id] = row
            else:
                failure_counts[row.conversation_id] = failure_counts.get(row.conversation_id, 0) + 1

        rows: list[dict[str, Any]] = []
        for conv in conversations:
            if conversation_id and conv.id != conversation_id:
                continue
            conv_messages = sorted(
                by_conversation.get(conv.id, []),
                key=lambda m: _ensure_utc(m.timestamp) or PROCESS_STARTED_AT,
            )
            payload = [{"role": m.role, "content": m.content} for m in conv_messages]
            context_tokens = count_prompt_tokens(payload) if payload else 0
            summary_row = last_summary.get(conv.id)
            rows.append({
                "conversation_id": conv.id,
                "title": conv.title,
                "character_name": conv.character_name,
                "message_count": len(conv_messages),
                "context_tokens_estimated": context_tokens,
                "context_limit": context_limit,
                "summarization_threshold": threshold,
                "threshold_tokens": int(context_limit * threshold),
                "context_pressure_pct": (
                    round((context_tokens / context_limit) * 100.0, 1) if context_limit else 0.0
                ),
                "has_stored_summary": conv.id in stored_summaries,
                "last_summary_at": _iso(summary_row.created_at) if summary_row else None,
                "summarization_failures": failure_counts.get(conv.id, 0),
                "updated_at": _iso(conv.updated_at),
            })

        rows.sort(key=lambda r: r["context_tokens_estimated"], reverse=True)
        return {
            "conversations": rows[: max(1, limit)],
            "context_limit": context_limit,
            "summarization_threshold": threshold,
            "note": "Context sizes are estimates (~4 characters per token).",
        }

    def get_memory_detail(self, conversation_id: str) -> dict[str, Any]:
        """Per-conversation context detail, including the stored summary text."""
        payload = self.get_memory(conversation_id=conversation_id, limit=1)
        conversations = payload["conversations"]
        if not conversations:
            raise KeyError(conversation_id)
        detail = dict(conversations[0])

        with self._get_session() as session:
            messages = sorted(
                session.exec(
                    select(MessageRow).where(MessageRow.conversation_id == conversation_id)
                ).all(),
                key=lambda m: _ensure_utc(m.timestamp) or PROCESS_STARTED_AT,
            )
            summaries = sorted(
                session.exec(
                    select(ConversationSummaryRow).where(
                        ConversationSummaryRow.conversation_id == conversation_id
                    )
                ).all(),
                key=lambda r: _ensure_utc(r.created_at) or PROCESS_STARTED_AT,
            )
        latest = summaries[-1] if summaries else None
        detail["stored_summary"] = latest.content if latest else None
        detail["summarized_messages"] = latest.covers_message_count if latest else 0
        detail["retained_messages"] = len(messages) - (
            latest.covers_message_count if latest else 0
        )
        detail["recent_messages"] = [
            {
                "role": m.role,
                "timestamp": _iso(m.timestamp),
                "characters": len(m.content),
            }
            for m in messages[-10:]
        ]
        return detail

    # ── schedules ────────────────────────────────────────────────────────

    def get_schedule_summary(self) -> dict[str, Any]:
        with self._get_session() as session:
            rows = list(session.exec(select(ScheduleInstanceRow)).all())
        now = datetime.now(UTC)
        upcoming = [
            _ensure_utc(r.next_run_at) for r in rows
            if r.enabled and _ensure_utc(r.next_run_at) is not None
        ]
        future = sorted(dt for dt in upcoming if dt is not None and dt >= now)
        counts = {"sent": 0, "skipped": 0, "failed": 0}
        for row in rows:
            if row.last_execution_status in counts:
                counts[row.last_execution_status] += 1
        return {
            "total": len(rows),
            "enabled": sum(1 for r in rows if r.enabled),
            "disabled": sum(1 for r in rows if not r.enabled),
            "next_run_at": _iso(future[0]) if future else None,
            "last_execution_status": counts,
            "execution_history": _registry.execution_counts(),
        }

    def get_schedules(
        self,
        *,
        status: str = "",
        enabled: bool | None = None,
        character_id: str = "",
        transport: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._get_session() as session:
            rows = list(session.exec(select(ScheduleInstanceRow)).all())
            conversations = {c.id: c for c in session.exec(select(ConversationRow)).all()}

        result: list[dict[str, Any]] = []
        for row in rows:
            if status and row.last_execution_status != status:
                continue
            if enabled is not None and row.enabled != enabled:
                continue
            if character_id and row.character_id != character_id:
                continue
            if transport and row.channel != transport:
                continue
            conversation = conversations.get(row.conversation_id)
            history = _registry.list_executions(schedule_id=row.id, limit=20)
            result.append({
                "id": row.id,
                "schedule_def_id": row.schedule_def_id,
                "character_id": row.character_id,
                "character_name": conversation.character_name if conversation else "",
                "conversation_id": row.conversation_id,
                "conversation_title": conversation.title if conversation else "",
                "transport": row.channel,
                "channel_instance_id": row.channel_instance_id,
                "user_ref": mask_identifier(row.external_user_id),
                "trigger": row.trigger_type,
                "origin": row.origin,
                "timezone": row.timezone,
                "enabled": row.enabled,
                "decision_mode": row.decision_mode,
                "next_run_at": _iso(row.next_run_at),
                "last_run_at": _iso(row.last_run_at),
                "last_sent_at": _iso(row.last_sent_at),
                "last_execution_at": _iso(row.last_execution_at),
                "last_execution_status": row.last_execution_status,
                "last_execution_reason": redact(row.last_execution_reason),
                "running": row.generation_started_at is not None,
                "execution_history": [e.to_dict() for e in history],
            })

        result.sort(key=lambda r: r["next_run_at"] or "~", reverse=False)
        return result[: max(1, limit)]

    # ── errors ───────────────────────────────────────────────────────────

    def get_errors(
        self,
        *,
        component: str = "",
        hours: int = 24,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        since = datetime.now(UTC) - timedelta(hours=max(1, hours))
        return [
            entry.to_dict()
            for entry in _registry.list_errors(component=component, since=since, limit=limit)
        ]
