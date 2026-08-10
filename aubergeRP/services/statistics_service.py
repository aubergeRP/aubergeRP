from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from ..db_models import ConversationRow, LLMCallStatRow, MessageRow


class StatisticsService:
    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        from ..database import init_db

        init_db(self._data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine

        return Session(get_engine(self._data_dir))

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt

    def record_text_call(
        self,
        *,
        conversation_id: str,
        connector_id: str,
        connector_name: str,
        connector_backend: str,
        request_tokens: int,
        response_tokens: int,
        response_time_ms: int,
        success: bool,
        error_detail: str = "",
        generation_type: str = "chat",
        model: str = "",
        tokens_estimated: bool = True,
    ) -> None:
        total = max(0, request_tokens) + max(0, response_tokens)
        row = LLMCallStatRow(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            connector_id=connector_id,
            connector_name=connector_name,
            connector_backend=connector_backend,
            request_tokens=max(0, request_tokens),
            response_tokens=max(0, response_tokens),
            total_tokens=total,
            response_time_ms=max(0, response_time_ms),
            success=success,
            error_detail=error_detail,
            generation_type=generation_type,
            model=model,
            tokens_estimated=tokens_estimated,
            created_at=datetime.now(UTC),
        )
        with self._get_session() as session:
            session.add(row)
            session.commit()

    def get_dashboard_data(self, days: int = 14, top: int = 15) -> dict[str, Any]:
        with self._get_session() as session:
            conversations = list(session.exec(select(ConversationRow)).all())
            messages = list(session.exec(select(MessageRow)).all())
            calls = list(session.exec(select(LLMCallStatRow)).all())

        total_messages = len(messages)
        total_conversations = len(conversations)
        total_calls = len(calls)
        total_in = sum(max(0, c.request_tokens) for c in calls)
        total_out = sum(max(0, c.response_tokens) for c in calls)
        total_tokens = total_in + total_out
        total_latency = sum(max(0, c.response_time_ms) for c in calls)
        successful_calls = sum(1 for c in calls if c.success)
        failed_calls = total_calls - successful_calls

        avg_latency = round(total_latency / total_calls, 1) if total_calls else 0.0
        success_rate = round((successful_calls / total_calls) * 100.0, 1) if total_calls else 0.0

        conv_title: dict[str, str] = {c.id: c.title for c in conversations}
        conv_message_count: dict[str, int] = defaultdict(int)
        for msg in messages:
            conv_message_count[msg.conversation_id] += 1

        by_conversation_raw: dict[str, dict[str, Any]] = {}
        for call in calls:
            cid = call.conversation_id
            row = by_conversation_raw.setdefault(
                cid,
                {
                    "conversation_id": cid,
                    "title": conv_title.get(cid) or cid,
                    "message_count": conv_message_count.get(cid, 0),
                    "llm_calls": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "latency_total_ms": 0,
                },
            )
            row["llm_calls"] += 1
            row["tokens_in"] += max(0, call.request_tokens)
            row["tokens_out"] += max(0, call.response_tokens)
            row["total_tokens"] += max(0, call.total_tokens)
            row["latency_total_ms"] += max(0, call.response_time_ms)

        by_conversation: list[dict[str, Any]] = []
        for conv_id, row in by_conversation_raw.items():
            calls_count = max(1, row["llm_calls"])
            by_conversation.append(
                {
                    "conversation_id": conv_id,
                    "title": row["title"],
                    "message_count": row["message_count"],
                    "llm_calls": row["llm_calls"],
                    "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"],
                    "total_tokens": row["total_tokens"],
                    "avg_latency_ms": round(row["latency_total_ms"] / calls_count, 1),
                }
            )
        by_conversation.sort(key=lambda item: (item["total_tokens"], item["llm_calls"]), reverse=True)

        by_connector_raw: dict[tuple[str, str, str], dict[str, Any]] = {}
        for call in calls:
            key = (
                call.connector_id or "",
                call.connector_name or "(unknown)",
                call.connector_backend or "(unknown)",
            )
            row = by_connector_raw.setdefault(
                key,
                {
                    "connector_id": key[0],
                    "name": key[1],
                    "backend": key[2],
                    "llm_calls": 0,
                    "success": 0,
                    "failed": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "total_tokens": 0,
                    "latency_total_ms": 0,
                },
            )
            row["llm_calls"] += 1
            row["success"] += 1 if call.success else 0
            row["failed"] += 0 if call.success else 1
            row["tokens_in"] += max(0, call.request_tokens)
            row["tokens_out"] += max(0, call.response_tokens)
            row["total_tokens"] += max(0, call.total_tokens)
            row["latency_total_ms"] += max(0, call.response_time_ms)

        by_connector: list[dict[str, Any]] = []
        for row in by_connector_raw.values():
            calls_count = max(1, row["llm_calls"])
            by_connector.append(
                {
                    "connector_id": row["connector_id"],
                    "name": row["name"],
                    "backend": row["backend"],
                    "llm_calls": row["llm_calls"],
                    "success": row["success"],
                    "failed": row["failed"],
                    "tokens_in": row["tokens_in"],
                    "tokens_out": row["tokens_out"],
                    "total_tokens": row["total_tokens"],
                    "avg_latency_ms": round(row["latency_total_ms"] / calls_count, 1),
                }
            )
        by_connector.sort(key=lambda item: (item["total_tokens"], item["llm_calls"]), reverse=True)

        today = datetime.now(UTC).date()
        start = today - timedelta(days=max(1, days) - 1)
        timeline_map: dict[str, dict[str, Any]] = {}
        for i in range(max(1, days)):
            day = start + timedelta(days=i)
            day_key = day.isoformat()
            timeline_map[day_key] = {
                "date": day_key,
                "llm_calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
            }

        for call in calls:
            day_key = self._ensure_utc(call.created_at).date().isoformat()
            if day_key not in timeline_map:
                continue
            timeline_map[day_key]["llm_calls"] += 1
            timeline_map[day_key]["tokens_in"] += max(0, call.request_tokens)
            timeline_map[day_key]["tokens_out"] += max(0, call.response_tokens)

        return {
            "summary": {
                "total_conversations": total_conversations,
                "total_messages": total_messages,
                "llm_calls": total_calls,
                "successful_calls": successful_calls,
                "failed_calls": failed_calls,
                "success_rate": success_rate,
                "tokens_in": total_in,
                "tokens_out": total_out,
                "total_tokens": total_tokens,
                "avg_latency_ms": avg_latency,
            },
            "timeline": list(timeline_map.values()),
            "by_connector": by_connector[: max(1, top)],
            "by_conversation": by_conversation[: max(1, top)],
            "generated_at": datetime.now(UTC).isoformat(),
            "range_days": max(1, days),
        }
