"""ChannelSessionService — transport-neutral external-user → AubergeRP conversation mapping."""
from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, and_, select

from ..db_models import ChannelSessionRow
from ..models.conversation import Conversation
from ..services.character_service import CharacterService
from ..services.conversation_service import ConversationService


class ChannelSessionService:
    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    def get_or_create(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        external_chat_id: str,
        character_id: str,
    ) -> tuple[str, bool]:
        """Return (conversation_id, created).

        If no existing mapping is found, a new AubergeRP conversation is created
        and persisted using the provided *character_id*.

        The unique index on (channel, channel_instance_id, external_user_id)
        prevents duplicate rows even under concurrent access; an IntegrityError
        on insert is handled by re-fetching the winning row.
        """
        # First, fast-path check (no new conversation created).
        with self._get_session() as session:
            row = session.exec(
                select(ChannelSessionRow).where(
                    and_(
                        ChannelSessionRow.channel == channel,
                        ChannelSessionRow.channel_instance_id == channel_instance_id,
                        ChannelSessionRow.external_user_id == external_user_id,
                    )
                )
            ).first()
            if row is not None:
                return row.conversation_id, False

        # Create AubergeRP conversation and attempt to insert the mapping.
        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv = conv_svc.create_conversation(character_id=character_id)

        now = datetime.now(UTC)
        mapping = ChannelSessionRow(
            id=str(uuid.uuid4()),
            channel=channel,
            channel_instance_id=channel_instance_id,
            external_user_id=external_user_id,
            external_chat_id=external_chat_id,
            conversation_id=conv.id,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._get_session() as session:
                session.add(mapping)
                session.commit()
            return conv.id, True
        except IntegrityError:
            # A concurrent request already inserted a row for this user+bot.
            # Delete the orphaned conversation we just created and return the winner.
            with contextlib.suppress(Exception):
                conv_svc.delete_conversation(conv.id)
            with self._get_session() as session:
                row = session.exec(
                    select(ChannelSessionRow).where(
                        and_(
                            ChannelSessionRow.channel == channel,
                            ChannelSessionRow.channel_instance_id == channel_instance_id,
                            ChannelSessionRow.external_user_id == external_user_id,
                        )
                    )
                ).first()
                if row is not None:
                    return row.conversation_id, False
            # Fallback: return our own conversation id (very unlikely).
            return conv.id, True

    def reset(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        external_chat_id: str,
        character_id: str,
    ) -> str:
        """Create a new conversation and update the mapping.  Returns new conversation_id."""
        char_svc = CharacterService(data_dir=self._data_dir)
        conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
        conv: Conversation = conv_svc.create_conversation(character_id=character_id)

        now = datetime.now(UTC)
        try:
            with self._get_session() as session:
                row = session.exec(
                    select(ChannelSessionRow).where(
                        and_(
                            ChannelSessionRow.channel == channel,
                            ChannelSessionRow.channel_instance_id == channel_instance_id,
                            ChannelSessionRow.external_user_id == external_user_id,
                        )
                    )
                ).first()
                if row is not None:
                    row.conversation_id = conv.id
                    row.updated_at = now
                    session.add(row)
                else:
                    session.add(ChannelSessionRow(
                        id=str(uuid.uuid4()),
                        channel=channel,
                        channel_instance_id=channel_instance_id,
                        external_user_id=external_user_id,
                        external_chat_id=external_chat_id,
                        conversation_id=conv.id,
                        created_at=now,
                        updated_at=now,
                    ))
                session.commit()
        except IntegrityError:
            # Another request inserted the mapping concurrently; re-fetch and update it.
            with self._get_session() as session:
                row = session.exec(
                    select(ChannelSessionRow).where(
                        and_(
                            ChannelSessionRow.channel == channel,
                            ChannelSessionRow.channel_instance_id == channel_instance_id,
                            ChannelSessionRow.external_user_id == external_user_id,
                        )
                    )
                ).first()
                if row is not None:
                    row.conversation_id = conv.id
                    row.updated_at = now
                    session.add(row)
                    session.commit()

        return conv.id

    def get_conversation_id(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
    ) -> str | None:
        with self._get_session() as session:
            row = session.exec(
                select(ChannelSessionRow).where(
                    and_(
                        ChannelSessionRow.channel == channel,
                        ChannelSessionRow.channel_instance_id == channel_instance_id,
                        ChannelSessionRow.external_user_id == external_user_id,
                    )
                )
            ).first()
            return row.conversation_id if row else None
