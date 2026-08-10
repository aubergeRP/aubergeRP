from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from ..models.character import CharacterCard, CharacterData, CharacterSummary
from ..services.character_service import (
    CharacterImportError,
    CharacterNotFoundError,
    CharacterService,
)
from .admin import get_admin_token

router = APIRouter(prefix="/characters", tags=["characters"])


def get_character_service() -> CharacterService:
    from ..config import get_config
    config = get_config()
    return CharacterService(data_dir=config.app.data_dir)


def _not_found(character_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Character '{character_id}' not found")


# Fixed routes BEFORE parameterised ones to avoid shadowing
@router.post("/import", status_code=201)
async def import_character(
    file: UploadFile = File(...),
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> CharacterCard:
    content = await file.read()
    filename = file.filename or ""
    try:
        if filename.lower().endswith(".png"):
            card = service.import_character_png(content)
        else:
            card = service.import_character_json(content, filename)
    except CharacterImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return card


@router.get("/")
def list_characters(service: CharacterService = Depends(get_character_service)) -> list[CharacterSummary]:
    return service.list_characters()


@router.post("/", status_code=201)
def create_character(
    data: CharacterData,
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> CharacterCard:
    return service.create_character(data)


@router.get("/{character_id}")
def get_character(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
) -> CharacterCard:
    try:
        return service.get_character(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)


@router.put("/{character_id}")
def update_character(
    character_id: str,
    data: CharacterData,
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> CharacterCard:
    try:
        return service.update_character(character_id, data)
    except CharacterNotFoundError:
        raise _not_found(character_id)


@router.delete("/{character_id}", status_code=204)
def delete_character(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> None:
    # Prevent silent deletion when a Telegram bot still references this character.
    from ..config import get_config
    from ..services.telegram_bot_service import TelegramBotService
    tg_svc = TelegramBotService(data_dir=get_config().app.data_dir)
    if tg_svc.character_is_referenced(character_id):
        raise HTTPException(
            status_code=409,
            detail="Character is referenced by one or more Telegram bots. "
                   "Reassign or delete those bots first.",
        )
    try:
        service.delete_character(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)


@router.get("/{character_id}/avatar")
def get_avatar(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
) -> FileResponse:
    try:
        service.get_character(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)
    path = service.get_avatar_path(character_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No avatar set for this character")
    return FileResponse(path)


@router.post("/{character_id}/avatar")
async def upload_avatar(
    character_id: str,
    file: UploadFile = File(...),
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> dict[str, str]:
    try:
        service.get_character(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)
    content = await file.read()
    service.save_avatar(character_id, content)
    return {"avatar_url": f"/api/characters/{character_id}/avatar"}


@router.get("/{character_id}/export/json")
def export_json(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
) -> Response:
    try:
        data = service.export_character_json(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)
    name = data.get("data", {}).get("name", "character")
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
    )


@router.get("/{character_id}/export/png")
def export_png(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
) -> Response:
    try:
        content = service.export_character_png(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)
    except (CharacterImportError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=content,
        media_type="image/png",
        headers={"Content-Disposition": 'attachment; filename="character.png"'},
    )


@router.post("/{character_id}/duplicate", status_code=201)
def duplicate_character(
    character_id: str,
    service: CharacterService = Depends(get_character_service),
    admin_token: str = Depends(get_admin_token),
) -> CharacterCard:
    try:
        return service.duplicate_character(character_id)
    except CharacterNotFoundError:
        raise _not_found(character_id)
