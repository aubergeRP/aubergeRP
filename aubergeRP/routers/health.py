from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import __version__
from ..config import get_config
from ..connectors.manager import ConnectorManager
from .connectors import _last_test_results, get_connector_manager

router = APIRouter(prefix="/health", tags=["health"])


def _connector_info(manager: ConnectorManager, connector_id: str | None) -> dict[str, Any] | None:
    if not connector_id:
        return None
    try:
        instance = manager.get_connector(connector_id)
    except KeyError:
        return None
    return {
        "id": instance.id,
        "name": instance.name,
        # Last known state — refreshed by the background health checker.
        "connected": _last_test_results.get(connector_id, None),
        "checked_at": _last_test_results.get_checked_at(connector_id),
    }


@router.get("/")
def health() -> dict[str, Any]:
    config = get_config()
    manager = get_connector_manager()
    text_id = config.active_connectors.text or None
    image_id = config.active_connectors.image or None
    return {
        "status": "ok",
        "version": __version__,
        "connectors": {
            "text": _connector_info(manager, text_id),
            "image": _connector_info(manager, image_id),
            "video": None,
            "audio": None,
        },
    }
