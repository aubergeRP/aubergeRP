"""Admin-only operational observability endpoints.

Everything here is read-only aggregation over data the application already
keeps (see :mod:`aubergeRP.services.observability_service`).  No secret is ever
returned: tokens, API keys and webhook secrets are scrubbed by ``redact``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..services.observability_service import ObservabilityService
from .admin import get_admin_token

router = APIRouter(prefix="/observability", tags=["observability"])


def get_observability_service() -> ObservabilityService:
    from ..config import get_config

    return ObservabilityService(data_dir=get_config().app.data_dir)


@router.get("/overview")
def get_overview(
    hours: int = Query(default=24, ge=1, le=720),
    _token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    """Headline health numbers for every dashboard section."""
    return get_observability_service().get_overview(hours=hours)


@router.get("/telegram")
def get_telegram(
    _token: str = Depends(get_admin_token),
) -> list[dict[str, Any]]:
    """Configuration + runtime state of every configured Telegram bot."""
    return get_observability_service().get_telegram_bots()


@router.get("/telegram/{bot_id}/webhook")
async def get_telegram_webhook(
    bot_id: str,
    _token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    """Live webhook information as reported by Telegram."""
    try:
        return await get_observability_service().get_webhook_info(bot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Bot not found") from None


@router.get("/sessions")
def get_sessions(
    transport: str = Query(default=""),
    bot_id: str = Query(default=""),
    character_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    _token: str = Depends(get_admin_token),
) -> list[dict[str, Any]]:
    """Recent/active sessions across every transport."""
    return get_observability_service().get_sessions(
        transport=transport, bot_id=bot_id, character_id=character_id, limit=limit
    )


@router.get("/llm")
def get_llm(
    hours: int = Query(default=24, ge=1, le=720),
    generation_type: str = Query(default=""),
    conversation_id: str = Query(default=""),
    success: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    _token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    """LLM generation aggregates and the most recent calls."""
    return get_observability_service().get_llm(
        hours=hours,
        generation_type=generation_type,
        conversation_id=conversation_id,
        success=success,
        limit=limit,
    )


@router.get("/memory")
def get_memory(
    limit: int = Query(default=50, ge=1, le=500),
    conversation_id: str = Query(default=""),
    _token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    """Estimated context pressure and summarization state per conversation."""
    return get_observability_service().get_memory(limit=limit, conversation_id=conversation_id)


@router.get("/memory/{conversation_id}")
def get_memory_detail(
    conversation_id: str,
    _token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    """Context detail for one conversation, including its stored summary."""
    try:
        return get_observability_service().get_memory_detail(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None


@router.get("/schedules")
def get_schedules(
    status: str = Query(default=""),
    enabled: bool | None = Query(default=None),
    character_id: str = Query(default=""),
    transport: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    _token: str = Depends(get_admin_token),
) -> list[dict[str, Any]]:
    """Proactive schedule instances with their recent execution history."""
    return get_observability_service().get_schedules(
        status=status,
        enabled=enabled,
        character_id=character_id,
        transport=transport,
        limit=limit,
    )


@router.get("/errors")
def get_errors(
    component: str = Query(default=""),
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    _token: str = Depends(get_admin_token),
) -> list[dict[str, Any]]:
    """Recent operational errors, newest first, already redacted."""
    return get_observability_service().get_errors(
        component=component, hours=hours, limit=limit
    )
