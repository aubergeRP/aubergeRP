from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, func, select

from ..db_models import ConversationRow, MessageRow
from ..models.conversation import Conversation, ConversationSummary, Message
from ..services.character_service import CharacterService

_MACRO_RE = re.compile(r"\{\{(\w+)\}\}")


class ConversationNotFoundError(KeyError):
    pass


def resolve_macros(text: str, char_name: str, user_name: str = "User", **extra: str) -> str:
    mapping: dict[str, str] = {"char": char_name, "user": user_name, **extra}
    return _MACRO_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). SQLite strips tzinfo."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _build_messages(msg_rows: list[MessageRow]) -> list[Message]:
    """Build Message objects from DB rows (call while session is still open)."""
    return [
        Message(
            id=m.id,
            role=m.role,  # type: ignore[arg-type]
            content=m.content,
            images=m.get_images(),
            timestamp=_ensure_utc(m.timestamp),
        )
        for m in msg_rows
    ]


def _build_conv(conv_row: ConversationRow, messages: list[Message]) -> Conversation:
    return Conversation(
        id=conv_row.id,
        character_id=conv_row.character_id,
        character_name=conv_row.character_name,
        title=conv_row.title,
        owner=conv_row.owner,
        messages=messages,
        created_at=_ensure_utc(conv_row.created_at),
        updated_at=_ensure_utc(conv_row.updated_at),
    )


class ConversationService:
    def __init__(
        self,
        data_dir: Path | str,
        character_service: CharacterService,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._character_service = character_service

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    @staticmethod
    def _summary_from_row(conv_row: ConversationRow, message_count: int) -> ConversationSummary:
        return ConversationSummary(
            id=conv_row.id,
            character_id=conv_row.character_id,
            character_name=conv_row.character_name,
            title=conv_row.title,
            owner=conv_row.owner,
            message_count=message_count,
            created_at=_ensure_utc(conv_row.created_at),
            updated_at=_ensure_utc(conv_row.updated_at),
        )

    @staticmethod
    def _summary(conv: Conversation) -> ConversationSummary:
        return ConversationSummary(
            id=conv.id,
            character_id=conv.character_id,
            character_name=conv.character_name,
            title=conv.title,
            owner=conv.owner,
            message_count=len(conv.messages),
            created_at=conv.created_at,
            updated_at=conv.updated_at,
        )

    def _load(self, conversation_id: str) -> Conversation:
        with self._get_session() as session:
            conv_row = session.get(ConversationRow, conversation_id)
            if conv_row is None:
                raise ConversationNotFoundError(
                    f"Conversation '{conversation_id}' not found"
                )
            msg_rows = list(session.exec(
                select(MessageRow)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.timestamp)  # type: ignore[arg-type]
            ).all())
            messages = _build_messages(msg_rows)
            return _build_conv(conv_row, messages)

    @staticmethod
    def _should_include(
        conv_row: ConversationRow,
        character_id: str | None,
        owner: str | None,
    ) -> bool:
        if character_id is not None and conv_row.character_id != character_id:
            return False
        return not (owner and conv_row.owner and conv_row.owner != owner)

    def create_conversation(
        self, character_id: str, user_name: str = "User", owner: str = ""
    ) -> Conversation:
        char = self._character_service.get_character(character_id)
        now = datetime.now(UTC)
        conv_id = str(uuid.uuid4())

        title = f"{char.data.name} — {now.strftime('%Y-%m-%d %H:%M')}"
        conv_row = ConversationRow(
            id=conv_id,
            character_id=character_id,
            character_name=char.data.name,
            title=title,
            owner=owner,
            created_at=now,
            updated_at=now,
        )

        msg_rows: list[MessageRow] = []
        if char.data.first_mes:
            content = resolve_macros(char.data.first_mes, char.data.name, user_name)
            msg_rows.append(MessageRow(
                id=str(uuid.uuid4()),
                conversation_id=conv_id,
                role="assistant",
                content=content,
                images_json="[]",
                timestamp=now,
            ))

        with self._get_session() as session:
            session.add(conv_row)
            for m in msg_rows:
                session.add(m)
            session.commit()
            # Reload from session to get fresh data
            session.refresh(conv_row)
            for m in msg_rows:
                session.refresh(m)
            messages = _build_messages(msg_rows)
            return _build_conv(conv_row, messages)

    def get_conversation(self, conversation_id: str) -> Conversation:
        return self._load(conversation_id)

    def list_conversations(
        self, character_id: str | None = None, owner: str | None = None
    ) -> list[ConversationSummary]:
        result = []
        with self._get_session() as session:
            conv_rows = list(session.exec(select(ConversationRow)).all())

            # Fetch message counts for all conversations in a single query.
            count_rows = session.exec(
                select(MessageRow.conversation_id, func.count(MessageRow.id).label("message_count"))  # type: ignore[arg-type]
                .group_by(MessageRow.conversation_id)
            ).all()
            counts: dict[str, int] = {row[0]: row[1] for row in count_rows}

            for conv_row in conv_rows:
                if not self._should_include(conv_row, character_id, owner):
                    continue
                result.append(
                    self._summary_from_row(conv_row, counts.get(conv_row.id, 0))
                )
        return result

    def delete_conversation(self, conversation_id: str, owner: str = "") -> None:
        conv = self._load(conversation_id)  # raises if not found
        # If an owner token is provided and the conversation has one, they must match.
        if owner and conv.owner and conv.owner != owner:
            raise ConversationNotFoundError(
                f"Conversation '{conversation_id}' not found"
            )
        with self._get_session() as session:
            msg_rows = list(session.exec(
                select(MessageRow).where(MessageRow.conversation_id == conversation_id)
            ).all())
            for m in msg_rows:
                session.delete(m)
            conv_row = session.get(ConversationRow, conversation_id)
            if conv_row is not None:
                session.delete(conv_row)
            session.commit()

    def append_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        images: list[str] | None = None,
    ) -> Message:
        self._load(conversation_id)  # validates existence
        now = datetime.now(UTC)
        msg_id = str(uuid.uuid4())
        msg_row = MessageRow(
            id=msg_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            images_json=MessageRow.images_to_json(images or []),
            timestamp=now,
        )
        with self._get_session() as session:
            session.add(msg_row)
            conv_row = session.get(ConversationRow, conversation_id)
            if conv_row is not None:
                conv_row.updated_at = now
                session.add(conv_row)
            session.commit()

        # Build Message from pre-captured values (msg_row is detached after session closes)
        return Message(
            id=msg_id,
            role=role,  # type: ignore[arg-type]
            content=content,
            images=images or [],
            timestamp=now,
        )

    def delete_message(self, conversation_id: str, message_id: str) -> None:
        self._load(conversation_id)  # validates existence
        with self._get_session() as session:
            msg_row = session.get(MessageRow, message_id)
            if msg_row is None or msg_row.conversation_id != conversation_id:
                raise KeyError(f"Message '{message_id}' not found")

            session.delete(msg_row)
            conv_row = session.get(ConversationRow, conversation_id)
            if conv_row is not None:
                latest_row = session.exec(
                    select(MessageRow.timestamp)
                    .where(
                        MessageRow.conversation_id == conversation_id,
                        MessageRow.id != message_id,
                    )
                    .order_by(MessageRow.timestamp.desc())  # type: ignore[arg-type]
                ).first()
                conv_row.updated_at = latest_row or conv_row.created_at
                session.add(conv_row)
            session.commit()
