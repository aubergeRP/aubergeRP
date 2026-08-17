from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlmodel import Session, func, select

from ..db_models import ConversationRow, MediaRow, MessageRow


class MediaNotFoundError(KeyError):
    pass


@dataclass(slots=True)
class GeneratedMedia:
    """One generated media plus the full trace of how its prompt was built.

    Every step is kept so the admin media page can show whether the character
    prefix and negative prompt were really applied.
    """

    media_url: str
    # Final prompt sent to the image connector (prefix included).
    prompt: str = ""
    # Keywords emitted by the roleplay LLM ("" for a standalone scene image).
    raw_prompt: str = ""
    # Filled-in image_prompt template sent to the text_utility connector.
    llm_input_prompt: str = ""
    # Cleaned answer of that connector.
    llm_output_prompt: str = ""
    prompt_prefix: str = ""
    negative_prompt: str = ""
    connector_name: str = ""

    def as_columns(self) -> dict[str, str]:
        """Return the prompt-trace fields as MediaRow keyword arguments."""
        return {
            "prompt": self.prompt,
            "raw_prompt": self.raw_prompt,
            "llm_input_prompt": self.llm_input_prompt,
            "llm_output_prompt": self.llm_output_prompt,
            "prompt_prefix": self.prompt_prefix,
            "negative_prompt": self.negative_prompt,
            "connector_name": self.connector_name,
        }

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> GeneratedMedia:
        """Build from an ``image_complete`` event emitted by ChatService."""
        details = event.get("details") or {}
        known = {f.name for f in fields(cls)} - {"media_url"}
        kwargs = {k: str(v or "") for k, v in details.items() if k in known}
        kwargs["media_url"] = str(event.get("image_url") or "")
        kwargs.setdefault("prompt", str(event.get("prompt") or ""))
        return cls(**kwargs)


class MediaService:
    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine

        return Session(get_engine(self._data_dir))

    def list_media(
        self,
        page: int = 1,
        per_page: int = 50,
        media_type: str | None = None,
    ) -> tuple[list[MediaRow], int]:
        with self._get_session() as session:
            created_at_expr = cast(Any, MediaRow.created_at)

            count_query = select(func.count()).select_from(MediaRow)
            if media_type:
                count_query = count_query.where(MediaRow.media_type == media_type)
            total = session.exec(count_query).one()

            rows_query = select(MediaRow).order_by(created_at_expr.desc())
            if media_type:
                rows_query = rows_query.where(MediaRow.media_type == media_type)
            offset = (page - 1) * per_page
            rows = list(session.exec(rows_query.offset(offset).limit(per_page)))
            return rows, total

    def record_generated_media(
        self,
        conversation_id: str,
        message_id: str,
        media_items: list[GeneratedMedia],
    ) -> None:
        if not media_items:
            return

        with self._get_session() as session:
            conv = session.get(ConversationRow, conversation_id)
            owner = conv.owner if conv is not None else ""
            now = datetime.now(UTC)

            for item in media_items:
                if not item.media_url:
                    continue
                row = MediaRow(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    message_id=message_id,
                    owner=owner,
                    media_type=_infer_media_type(item.media_url),
                    media_url=item.media_url,
                    generated_via_connector=True,
                    created_at=now,
                    **item.as_columns(),
                )
                session.add(row)
            session.commit()

    def delete_media(self, media_id: str) -> None:
        with self._get_session() as session:
            row = session.get(MediaRow, media_id)
            if row is None:
                raise MediaNotFoundError(f"Media '{media_id}' not found")

            duplicate_count = len(
                list(
                    session.exec(
                        select(MediaRow.id).where(
                            MediaRow.media_url == row.media_url,
                            MediaRow.id != row.id,
                        )
                    )
                )
            )

            message = session.get(MessageRow, row.message_id) if row.message_id else None
            if message is not None:
                try:
                    images = json.loads(message.images_json or "[]")
                except Exception:
                    images = []
                if isinstance(images, list):
                    message.images_json = json.dumps(
                        [img for img in images if img != row.media_url], ensure_ascii=False
                    )
                    session.add(message)

            session.delete(row)
            session.commit()

        if duplicate_count == 0:
            file_path = _resolve_local_media_path(self._data_dir, row.media_url)
            if file_path is not None and file_path.exists():
                file_path.unlink()


def _infer_media_type(media_url: str) -> str:
    lower = media_url.lower()
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if lower.endswith((".mp4", ".webm", ".ogg", ".mov")):
        return "video"
    if lower.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
        return "audio"
    return "image"


def _resolve_local_media_path(data_dir: Path, media_url: str) -> Path | None:
    if not media_url.startswith("/api/images/"):
        return None

    parts = media_url.strip("/").split("/")
    # /api/images/{session_token}/{filename}
    if len(parts) != 4 or parts[0] != "api" or parts[1] != "images":
        return None

    session_token = parts[2]
    filename = parts[3]
    if not session_token or not filename:
        return None

    candidate = (data_dir / "images" / session_token / filename).resolve()
    images_root = (data_dir / "images").resolve()
    try:
        inside_root = os.path.commonpath([str(images_root), str(candidate)]) == str(images_root)
    except ValueError:
        inside_root = False

    if not inside_root:
        return None
    return candidate
