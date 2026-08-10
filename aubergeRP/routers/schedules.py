"""REST API for schedule instances (runtime execution state).

Character card schedule *definitions* are managed through the normal
character create/update endpoints (PUT /api/characters/{id}).

These endpoints manage per-conversation runtime *instances*:
- GET  /api/schedules/instances/character/{character_id}   — list
- GET  /api/schedules/instances/conversation/{conv_id}     — list
- GET  /api/schedules/instances/{id}                       — get
- POST /api/schedules/instances                            — create / get-or-create
- PATCH /api/schedules/instances/{id}/enabled              — enable / disable
- DELETE /api/schedules/instances/{id}                     — delete
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..models.character import ScheduleDefinition
from ..services.schedule_instance_service import (
    ScheduleInstancePublic,
    ScheduleInstanceService,
)
from .admin import get_admin_token

router = APIRouter(prefix="/schedules", tags=["schedules"])


def get_schedule_instance_service() -> ScheduleInstanceService:
    from ..config import get_config

    config = get_config()
    return ScheduleInstanceService(data_dir=config.app.data_dir)


class ScheduleInstanceCreate(BaseModel):
    """Request body for creating / getting a schedule instance."""

    schedule_def: ScheduleDefinition
    character_id: str
    conversation_id: str
    channel: str
    channel_instance_id: str
    external_user_id: str
    external_chat_id: str = ""
    timezone: str = "UTC"


class EnabledUpdate(BaseModel):
    enabled: bool


@router.get("/instances/character/{character_id}")
def list_for_character(
    character_id: str,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
) -> list[ScheduleInstancePublic]:
    return svc.list_for_character(character_id)


@router.get("/instances/conversation/{conversation_id}")
def list_for_conversation(
    conversation_id: str,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
) -> list[ScheduleInstancePublic]:
    return svc.list_for_conversation(conversation_id)


@router.get("/instances/{instance_id}")
def get_instance(
    instance_id: str,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
) -> ScheduleInstancePublic:
    try:
        return svc.get_instance(instance_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Schedule instance '{instance_id}' not found")


@router.post("/instances", status_code=201)
def create_instance(
    body: ScheduleInstanceCreate,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
    admin_token: str = Depends(get_admin_token),
) -> ScheduleInstancePublic:
    try:
        instance, _ = svc.get_or_create(
            defn=body.schedule_def,
            character_id=body.character_id,
            conversation_id=body.conversation_id,
            channel=body.channel,
            channel_instance_id=body.channel_instance_id,
            external_user_id=body.external_user_id,
            external_chat_id=body.external_chat_id,
            timezone=body.timezone,
        )
        return instance
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.patch("/instances/{instance_id}/enabled")
def set_enabled(
    instance_id: str,
    body: EnabledUpdate,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
    admin_token: str = Depends(get_admin_token),
) -> ScheduleInstancePublic:
    try:
        return svc.set_enabled(instance_id, body.enabled)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Schedule instance '{instance_id}' not found")


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(
    instance_id: str,
    svc: ScheduleInstanceService = Depends(get_schedule_instance_service),
    admin_token: str = Depends(get_admin_token),
) -> None:
    try:
        svc.delete_instance(instance_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Schedule instance '{instance_id}' not found")
