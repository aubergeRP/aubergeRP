from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..models.media import MediaItem, MediaPage
from ..services.media_service import MediaNotFoundError, MediaService
from .admin import get_admin_token

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/", response_model=MediaPage)
def list_media(
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    per_page: int = Query(default=50, ge=1, le=200, description="Items per page"),
    media_type: str | None = Query(default=None, description="Filter by media type (image, video, audio)"),
    admin_token: str = Depends(get_admin_token),
) -> MediaPage:
    from ..config import get_config

    config = get_config()
    service = MediaService(data_dir=config.app.data_dir)
    rows, total = service.list_media(page=page, per_page=per_page, media_type=media_type)
    items = [MediaItem.model_validate(row, from_attributes=True) for row in rows]
    return MediaPage(items=items, total=total, page=page, per_page=per_page)


@router.delete("/{media_id}", status_code=204)
def delete_media(
    media_id: str,
    admin_token: str = Depends(get_admin_token),
) -> None:
    from ..config import get_config

    config = get_config()
    service = MediaService(data_dir=config.app.data_dir)
    try:
        service.delete_media(media_id)
    except MediaNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
