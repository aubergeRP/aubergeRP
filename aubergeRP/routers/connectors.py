from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..connectors.manager import ConnectorManager
from ..models.chat import ConnectorTestChatRequest
from ..models.connector import (
    ConnectorCreate,
    ConnectorInstance,
    ConnectorResponse,
    ConnectorUpdate,
)
from .admin import get_admin_token
from .errors import config_write_error

router = APIRouter(prefix="/connectors", tags=["connectors"])


class _TestResultsStore:
    """Persistent connector-liveness store backed by a JSON sidecar file.

    Each entry is ``{"connected": bool, "checked_at": <iso8601>}``.  The file
    is kept mirrored in memory and only re-read from disk when its path or
    modification time changed, so hot readers (``/api/health``) do not pay for
    disk I/O on every call.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._path: Path | None = None
        self._mtime: float | None = None

    def _resolve_path(self) -> Path:
        from ..config import get_config
        data_dir = Path(get_config().app.data_dir)
        return data_dir / "connector_test_results.json"

    @staticmethod
    def _coerce(value: object) -> dict[str, Any] | None:
        """Normalise a raw JSON value into an entry dict (None if unusable)."""
        if isinstance(value, bool):  # legacy format: plain bool
            return {"connected": value, "checked_at": None}
        if isinstance(value, dict) and value.get("connected") is not None:
            return {
                "connected": bool(value.get("connected")),
                "checked_at": value.get("checked_at"),
            }
        return None

    def _load(self) -> None:
        """Refresh the in-memory mirror if the sidecar file changed on disk."""
        path = self._resolve_path()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if path == self._path and mtime == self._mtime:
            return
        self._path = path
        self._mtime = mtime
        if mtime is None:
            self._data = {}
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(raw, dict):
            entries = ((k, self._coerce(v)) for k, v in raw.items())
            self._data = {k: e for k, e in entries if e is not None}

    def get(self, connector_id: str, default: bool | None = None) -> bool | None:
        self._load()
        entry = self._data.get(connector_id)
        return default if entry is None else bool(entry["connected"])

    def get_checked_at(self, connector_id: str) -> str | None:
        """Return the ISO-8601 timestamp of the last check, if known."""
        self._load()
        entry = self._data.get(connector_id)
        return None if entry is None else entry.get("checked_at")

    def set(self, connector_id: str, value: bool) -> None:
        self._load()
        self._data[connector_id] = {
            "connected": bool(value),
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        self._persist()

    def pop(self, connector_id: str, default: object = None) -> object:
        self._load()
        removed = self._data.pop(connector_id, None)
        self._persist()
        return default if removed is None else bool(removed["connected"])

    def known_ids(self) -> list[str]:
        self._load()
        return list(self._data)

    def _persist(self) -> None:
        path = self._resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._path = path
        try:
            self._mtime = path.stat().st_mtime
        except OSError:
            self._mtime = None


# Module-level singleton — shared between connectors and health routers.
_last_test_results: _TestResultsStore = _TestResultsStore()

_manager_instance: ConnectorManager | None = None


def get_connector_manager() -> ConnectorManager:
    global _manager_instance
    if _manager_instance is None:
        from ..config import get_config
        config = get_config()
        _manager_instance = ConnectorManager(
            data_dir=config.app.data_dir,
            config=config,
        )
    return _manager_instance


def _redact(instance: ConnectorInstance, is_active: bool) -> ConnectorResponse:
    """Return ConnectorResponse with api_key replaced by api_key_set bool."""
    config = dict(instance.config)
    api_key_set = bool(config.pop("api_key", ""))
    nsfw_raw = config.get("nsfw", False)
    if isinstance(nsfw_raw, bool):
        nsfw = nsfw_raw
    elif isinstance(nsfw_raw, str):
        nsfw = nsfw_raw.strip().lower() in ("1", "true", "yes", "on")
    else:
        nsfw = bool(nsfw_raw)
    config["nsfw"] = nsfw
    config["api_key_set"] = api_key_set
    return ConnectorResponse(
        id=instance.id,
        name=instance.name,
        type=instance.type,
        backend=instance.backend,
        is_active=is_active,
        config=config,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )


def _not_found(connector_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Connector '{connector_id}' not found")


# Static routes MUST be declared before parameterised ones

@router.get("/backends")
def list_backends() -> list[dict[str, Any]]:
    return [
        {
            "id": "openai_api",
            "name": "OpenAI-Compatible API",
            "supported_types": ["text", "image"],
            "config_schema": {
                "common": {
                    "base_url": {"type": "string", "required": True},
                    "api_key": {"type": "string", "required": False},
                    "model": {"type": "string", "required": True},
                    "timeout": {"type": "number", "required": False},
                    "max_retries": {"type": "number", "required": False},
                    "nsfw": {"type": "boolean", "required": False},
                },
                "by_type": {
                    "text": {
                        "max_tokens": {"type": "number", "required": False},
                        "context_window": {"type": "number", "required": False},
                        "temperature": {"type": "number", "required": False},
                        "top_p": {"type": "number", "required": False},
                        "presence_penalty": {"type": "number", "required": False},
                        "frequency_penalty": {"type": "number", "required": False},
                        "extra_body": {"type": "object", "required": False},
                        "supports_tool_calling": {"type": "boolean", "required": False},
                    },
                    "image": {
                        "size": {"type": "string", "required": False},
                    },
                },
            },
        },
        {
            "id": "comfyui",
            "name": "ComfyUI",
            "supported_types": ["image"],
            "config_schema": {
                "common": {
                    "base_url": {"type": "string", "required": True},
                    "timeout": {"type": "number", "required": False},
                    "max_retries": {"type": "number", "required": False},
                    "nsfw": {"type": "boolean", "required": False},
                },
                "by_type": {
                    "image": {
                        "workflow": {"type": "workflow_select", "required": False},
                    },
                },
            },
        },
    ]


@router.get("/comfyui-workflows")
def list_comfyui_workflows(
    manager: ConnectorManager = Depends(get_connector_manager),
) -> list[str]:
    """List available ComfyUI workflow template names."""
    return manager.list_workflows()


@router.get("/")
def list_connectors(
    connector_type: str | None = Query(default=None, alias="type"),
    manager: ConnectorManager = Depends(get_connector_manager),
) -> list[ConnectorResponse]:
    instances = manager.list_connectors(connector_type=connector_type)
    return [_redact(i, manager.is_active(i.id)) for i in instances]


@router.post("/", status_code=201)
def create_connector(
    data: ConnectorCreate,
    manager: ConnectorManager = Depends(get_connector_manager),
    admin_token: str = Depends(get_admin_token),
) -> ConnectorResponse:
    if data.backend not in ("openai_api", "comfyui"):
        raise HTTPException(status_code=400, detail=f"Unknown backend '{data.backend}'")
    instance = manager.create_connector(data)
    # Auto-activate if no active connector of this type exists yet.
    if not manager.get_active_id_for_type(instance.type):
        # ValueError is raised for unsupported connector types (e.g. video/audio);
        # safe to skip auto-activation silently in that case.
        # OSError: config.yaml is read-only — the connector itself was created,
        # so only the activation is lost; surfacing it as a 500 would be wrong.
        with contextlib.suppress(ValueError, OSError):
            manager.set_active(instance.id)
    return _redact(instance, manager.is_active(instance.id))


@router.get("/{connector_id}")
def get_connector(
    connector_id: str,
    manager: ConnectorManager = Depends(get_connector_manager),
) -> ConnectorResponse:
    try:
        instance = manager.get_connector(connector_id)
    except KeyError:
        raise _not_found(connector_id)
    return _redact(instance, manager.is_active(connector_id))


@router.put("/{connector_id}")
def update_connector(
    connector_id: str,
    data: ConnectorUpdate,
    manager: ConnectorManager = Depends(get_connector_manager),
    admin_token: str = Depends(get_admin_token),
) -> ConnectorResponse:
    try:
        instance = manager.update_connector(connector_id, data)
    except KeyError:
        raise _not_found(connector_id)
    return _redact(instance, manager.is_active(connector_id))


@router.delete("/{connector_id}", status_code=204)
def delete_connector(
    connector_id: str,
    manager: ConnectorManager = Depends(get_connector_manager),
    admin_token: str = Depends(get_admin_token),
) -> None:
    try:
        manager.delete_connector(connector_id)
    except KeyError:
        raise _not_found(connector_id)
    except OSError as exc:
        raise config_write_error(exc) from None
    _last_test_results.pop(connector_id, None)


@router.post("/{connector_id}/test")
async def test_connector(
    connector_id: str,
    manager: ConnectorManager = Depends(get_connector_manager),
) -> dict[str, Any]:
    try:
        result = await manager.test_connector(connector_id)
    except KeyError:
        raise _not_found(connector_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    _last_test_results.set(connector_id, result.get("connected", False))
    return result


@router.post("/{connector_id}/test-chat")
async def test_connector_chat(
    connector_id: str,
    request: ConnectorTestChatRequest,
    manager: ConnectorManager = Depends(get_connector_manager),
) -> dict[str, Any]:
    """Test a text connector with a sample message and parameters.

    This endpoint tests the connector by sending a sample message with the
    specified sampling parameters and returns a sample of the response.
    Useful for validating that parameters like temperature, top_p, etc. are
    accepted by the connector.
    """
    try:
        instance = manager.get_connector(connector_id)
    except KeyError:
        raise _not_found(connector_id)

    if instance.type != "text":
        raise HTTPException(
            status_code=400,
            detail="Test chat only supported for text connectors"
        )

    try:
        conn = manager.build_connector(instance)
        from ..connectors.base import TextConnector
        if not isinstance(conn, TextConnector):
            raise HTTPException(
                status_code=400,
                detail="Connector is not a text connector"
            )

        # Prepare test message with system context
        messages = [
            {
                "role": "system",
                "content": "You are a helpful assistant. Respond concisely to test the connection."
            },
            {
                "role": "user",
                "content": request.message
            }
        ]

        # Build parameters for the test
        kwargs: dict[str, Any] = {}
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.top_p is not None:
            kwargs["top_p"] = request.top_p
        if request.presence_penalty is not None:
            kwargs["presence_penalty"] = request.presence_penalty
        if request.frequency_penalty is not None:
            kwargs["frequency_penalty"] = request.frequency_penalty
        if request.extra_body:
            kwargs["extra_body"] = request.extra_body

        # Stream the response and collect first 500 chars
        response_text = ""
        async for token in conn.stream_chat_completion(messages, **kwargs):
            response_text += token
            if len(response_text) >= 500:  # Limit response for test display
                response_text = response_text[:500]
                break

        return {
            "connected": True,
            "details": {
                "sample_response": response_text,
                "parameters_accepted": {
                    "temperature": request.temperature is not None,
                    "top_p": request.top_p is not None,
                    "presence_penalty": request.presence_penalty is not None,
                    "frequency_penalty": request.frequency_penalty is not None,
                    "extra_body": request.extra_body is not None,
                }
            }
        }
    except Exception as exc:
        return {
            "connected": False,
            "details": {"error": str(exc)}
        }



@router.post("/{connector_id}/activate")
def activate_connector(
    connector_id: str,
    manager: ConnectorManager = Depends(get_connector_manager),
    admin_token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    try:
        manager.set_active(connector_id)
        instance = manager.get_connector(connector_id)
    except KeyError:
        raise _not_found(connector_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise config_write_error(exc) from None
    return {"id": instance.id, "type": instance.type, "is_active": True}
