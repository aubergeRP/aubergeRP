"""Persisted, incremental conversation summaries.

Summaries used to be recomputed from the whole history on every single turn
and thrown away, which meant one extra LLM call per message and a summary that
changed under the model's feet.  Here a summary is produced once, stored, and
reused: the prompt becomes ``system + last summary + messages since``.  When
that grows past the budget again, the next summary is built from *the previous
summary plus the messages since*, so nothing is re-read twice.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, col, select

from ..db_models import ConversationSummaryRow
from ..models.conversation import Conversation, Message
from .summarization_service import (
    _MIN_RECENT_MESSAGES,
    _count_tokens,
    count_prompt_tokens,
    prompt_budget,
    summarize_excerpt,
)

if TYPE_CHECKING:
    from ..connectors.base import TextConnector
    from ..services.statistics_service import StatisticsService

logger = logging.getLogger(__name__)


class SummaryService:
    """Store and refresh the summaries of a conversation."""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    # -- reads --------------------------------------------------------------

    def get_latest(self, conversation_id: str) -> ConversationSummaryRow | None:
        """Return the most recent summary of a conversation, if any."""
        with self._get_session() as session:
            rows = list(session.exec(
                select(ConversationSummaryRow)
                .where(ConversationSummaryRow.conversation_id == conversation_id)
                .order_by(col(ConversationSummaryRow.created_at).desc())
                .limit(1)
            ).all())
            return rows[0] if rows else None

    def list_chain(self, conversation_id: str) -> list[ConversationSummaryRow]:
        """Return every summary of a conversation, oldest first."""
        with self._get_session() as session:
            return list(session.exec(
                select(ConversationSummaryRow)
                .where(ConversationSummaryRow.conversation_id == conversation_id)
                .order_by(col(ConversationSummaryRow.created_at))
            ).all())

    # -- writes -------------------------------------------------------------

    def delete_latest(self, conversation_id: str) -> bool:
        """Drop the most recent summary. Returns whether one was removed."""
        row = self.get_latest(conversation_id)
        if row is None:
            return False
        with self._get_session() as session:
            stored = session.get(ConversationSummaryRow, row.id)
            if stored is None:
                return False
            session.delete(stored)
            session.commit()
        return True

    def delete_all(self, conversation_id: str) -> int:
        """Drop every summary of a conversation. Returns the number removed."""
        with self._get_session() as session:
            rows = list(session.exec(
                select(ConversationSummaryRow)
                .where(ConversationSummaryRow.conversation_id == conversation_id)
            ).all())
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    # -- history splitting --------------------------------------------------

    def split_history(
        self,
        conversation: Conversation,
        summary: ConversationSummaryRow | None,
    ) -> tuple[str, list[Message]]:
        """Return ``(summary_text, messages_after_it)`` for *conversation*.

        When the summary points at a message that no longer exists (it was
        edited or deleted) the whole chain is dropped and the full history is
        returned — a stale summary is worse than no summary.
        """
        if summary is None:
            return "", list(conversation.messages)
        for index, msg in enumerate(conversation.messages):
            if msg.id == summary.covers_until_message_id:
                return summary.content, list(conversation.messages[index + 1:])
        logger.info(
            "Dropping stale summaries for conversation %s: message %s is gone",
            conversation.id, summary.covers_until_message_id,
        )
        self.delete_all(conversation.id)
        return "", list(conversation.messages)

    # -- summarizing --------------------------------------------------------

    async def summarize_now(
        self,
        conversation: Conversation,
        connector: TextConnector,
        *,
        keep_recent: int = _MIN_RECENT_MESSAGES,
        statistics_service: StatisticsService | None = None,
    ) -> ConversationSummaryRow | None:
        """Summarize everything but the *keep_recent* last messages.

        Returns the stored summary, or ``None`` when there was nothing to
        summarize or the LLM call failed (in which case the caller keeps using
        the previous state unchanged).
        """
        previous = self.get_latest(conversation.id)
        previous_text, history = self.split_history(conversation, previous)
        if previous_text == "":
            # split_history may have invalidated the chain.
            previous = None
        cutoff = max(0, len(history) - keep_recent)
        to_summarize = history[:cutoff]
        if not to_summarize:
            return None

        excerpt = [{"role": m.role, "content": m.content} for m in to_summarize]
        text = await summarize_excerpt(
            excerpt,
            connector,
            previous_summary=previous_text,
            conversation_id=conversation.id,
            statistics_service=statistics_service,
        )
        if text is None:
            return None

        row = ConversationSummaryRow(
            id=str(uuid.uuid4()),
            conversation_id=conversation.id,
            content=text,
            covers_until_message_id=to_summarize[-1].id,
            covers_message_count=(
                (previous.covers_message_count if previous else 0) + len(to_summarize)
            ),
            based_on_summary_id=previous.id if previous else "",
            tokens=_count_tokens(text),
            created_at=datetime.now(UTC),
        )
        with self._get_session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    # -- prompt assembly ----------------------------------------------------

    async def build_prompt_within_budget(
        self,
        conversation: Conversation,
        *,
        connector: TextConnector | None,
        context_window: int,
        threshold: float,
        statistics_service: StatisticsService | None = None,
        **build_kwargs: Any,
    ) -> list[dict[str, str]]:
        """Build the chat prompt, summarizing first if it exceeds the budget.

        This is the single entry point used by chat and proactive generation.
        Extra keyword arguments are forwarded to
        :func:`aubergeRP.services.chat_service.build_prompt`.
        """
        from .chat_service import build_prompt

        summary_text, history = self.split_history(
            conversation, self.get_latest(conversation.id)
        )
        messages = build_prompt(
            conversation, history=history, summary_text=summary_text or None, **build_kwargs
        )
        if connector is None:
            return messages
        if count_prompt_tokens(messages) <= prompt_budget(context_window, threshold):
            return messages

        row = await self.summarize_now(
            conversation, connector, statistics_service=statistics_service
        )
        if row is None:
            return messages
        summary_text, history = self.split_history(conversation, row)
        return build_prompt(
            conversation, history=history, summary_text=summary_text or None, **build_kwargs
        )
