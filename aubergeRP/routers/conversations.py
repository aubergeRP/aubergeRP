from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from ..models.conversation import (
    Conversation,
    ConversationCreate,
    ConversationSummary,
    Message,
    MessageCreate,
)
from ..services.character_service import CharacterNotFoundError, CharacterService
from ..services.conversation_service import ConversationNotFoundError, ConversationService
from ..services.schedule_instance_service import ScheduleInstanceService
from ..services.timezone_service import TimezoneService
from .admin import get_admin_token

router = APIRouter(prefix="/conversations", tags=["conversations"])
logger = logging.getLogger(__name__)


def get_conversation_service() -> ConversationService:
    from ..config import get_config
    config = get_config()
    char_svc = CharacterService(data_dir=config.app.data_dir)
    return ConversationService(data_dir=config.app.data_dir, character_service=char_svc)


def get_session_token(x_session_token: str = Header(default="")) -> str:
    return x_session_token


def _not_found(conversation_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Conversation '{conversation_id}' not found")


@router.get("/")
def list_conversations(
    character_id: str | None = None,
    service: ConversationService = Depends(get_conversation_service),
    session_token: str = Depends(get_session_token),
) -> list[ConversationSummary]:
    return service.list_conversations(character_id, owner=session_token)


@router.post("/", status_code=201)
def create_conversation(
    body: ConversationCreate,
    service: ConversationService = Depends(get_conversation_service),
    session_token: str = Depends(get_session_token),
) -> Conversation:
    try:
        conv = service.create_conversation(body.character_id, owner=session_token)
        from ..config import get_config
        from ..models.character import ProactiveConfig, ScheduleDefinition

        cfg = get_config()
        char = CharacterService(data_dir=cfg.app.data_dir).get_character(body.character_id)
        ext = char.data.extensions.get("aubergerp", {})
        schedules_raw = ext.get("schedules", []) if isinstance(ext, dict) else []
        proactive = ProactiveConfig(**(ext.get("proactive", {}) if isinstance(ext, dict) else {}))
        tz = TimezoneService(cfg.app.data_dir).get_timezone_name("web", "web", session_token) or "UTC"
        sched_svc = ScheduleInstanceService(cfg.app.data_dir)
        for raw in schedules_raw:
            if not isinstance(raw, dict):
                continue
            try:
                defn = ScheduleDefinition(**raw)
            except Exception:
                logger.warning(
                    "Invalid proactive schedule definition in character card '%s'",
                    body.character_id,
                    exc_info=True,
                )
                continue
            try:
                sched_svc.get_or_create(
                    defn=defn,
                    character_id=body.character_id,
                    conversation_id=conv.id,
                    channel="web",
                    channel_instance_id="web",
                    external_user_id=session_token or "web-user",
                    external_chat_id=session_token or "",
                    timezone=tz,
                    origin="character-card",
                    decision_mode=proactive.decision_mode,
                    proactive=proactive,
                )
            except Exception:
                logger.warning(
                    "Failed to create proactive schedule instance for conversation '%s'",
                    conv.id,
                    exc_info=True,
                )
                continue
        return conv
    except CharacterNotFoundError:
        raise HTTPException(status_code=404, detail=f"Character '{body.character_id}' not found")


@router.get("/admin/all")
def admin_list_conversations(
    character_id: str | None = None,
    service: ConversationService = Depends(get_conversation_service),
    admin_token: str = Depends(get_admin_token),
) -> list[ConversationSummary]:
    """List every conversation, regardless of the owning session."""
    convs = service.list_conversations(character_id, owner=None)
    convs.sort(key=lambda c: c.updated_at, reverse=True)
    return convs


@router.post("/admin/{conversation_id}/messages", status_code=201)
def admin_append_message(
    conversation_id: str,
    body: MessageCreate,
    service: ConversationService = Depends(get_conversation_service),
    admin_token: str = Depends(get_admin_token),
) -> Message:
    """Inject a message into a conversation history without calling the LLM."""
    try:
        return service.append_message(conversation_id, body.role, body.content)
    except ConversationNotFoundError:
        raise _not_found(conversation_id)


@router.delete("/admin/{conversation_id}/messages", status_code=204)
def admin_clear_messages(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
    admin_token: str = Depends(get_admin_token),
) -> None:
    """Wipe the whole history of a conversation, keeping the conversation."""
    try:
        service.clear_messages(conversation_id)
    except ConversationNotFoundError:
        raise _not_found(conversation_id)


@router.delete("/admin/{conversation_id}", status_code=204)
def admin_delete_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
    admin_token: str = Depends(get_admin_token),
) -> None:
    """Delete a conversation owned by any session."""
    try:
        service.delete_conversation(conversation_id)
    except ConversationNotFoundError:
        raise _not_found(conversation_id)


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
) -> Conversation:
    try:
        return service.get_conversation(conversation_id)
    except ConversationNotFoundError:
        raise _not_found(conversation_id)


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    service: ConversationService = Depends(get_conversation_service),
    session_token: str = Depends(get_session_token),
) -> None:
    try:
        service.delete_conversation(conversation_id, owner=session_token)
    except ConversationNotFoundError:
        raise _not_found(conversation_id)
