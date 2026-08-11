from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import yaml  # type: ignore[import-untyped]

from ..config import Config
from ..models.connector import (
    ComfyUIConfig,
    ConnectorCreate,
    ConnectorInstance,
    ConnectorUpdate,
    OpenAIImageConfig,
    OpenAITextConfig,
)
from ..utils.file_storage import read_json, write_json
from .base import BaseConnector, ImageConnector, TextConnector
from .comfyui import ComfyUIConnector
from .openai_image import OpenAIImageConnector
from .openai_text import OpenAITextConnector

logger = logging.getLogger(__name__)

_ConnectorT = TypeVar("_ConnectorT", bound=BaseConnector)


class ConnectorManager:
    def __init__(
        self,
        data_dir: Path | str,
        config: Config,
        config_path: Path | str = "config.yaml",
    ) -> None:
        self._data_dir = Path(data_dir)
        self._dir = self._data_dir / "connectors"
        self._config = config
        self._config_path = Path(config_path)
        self._connectors: dict[str, ConnectorInstance] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        if not self._dir.exists():
            logger.warning("Connectors directory not found: %s", self._dir.resolve())
            return
        files = sorted(self._dir.glob("*.json"))
        logger.info("Loading connectors from %s (%d file(s))", self._dir.resolve(), len(files))
        for path in files:
            try:
                data = read_json(path)
                instance = ConnectorInstance(**data)
                self._connectors[instance.id] = instance
                logger.info("  Loaded connector %s (%s / %s)", instance.id, instance.name, instance.type)
            except Exception:
                logger.warning("  Failed to load connector from %s", path, exc_info=True)

    def _save_instance(self, instance: ConnectorInstance) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        write_json(self._dir / f"{instance.id}.json", instance.model_dump(mode="json"))

    def _save_config(self) -> None:
        data = self._config.model_dump()
        with self._config_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    def _build_connector(self, instance: ConnectorInstance) -> BaseConnector:
        return self.build_connector(instance)

    def build_connector(self, instance: ConnectorInstance) -> BaseConnector:
        """Build and return the connector object for *instance*.

        Raises ``ValueError`` for unsupported backend/type combinations.
        """
        if instance.backend == "openai_api":
            if instance.type == "text":
                return OpenAITextConnector(OpenAITextConfig(**instance.config))
            if instance.type == "image":
                return OpenAIImageConnector(OpenAIImageConfig(**instance.config))
        if instance.backend == "comfyui" and instance.type == "image":
            workflows_dir = self._data_dir / "comfyui_workflows"
            return ComfyUIConnector(ComfyUIConfig(**instance.config), workflows_dir)
        raise ValueError(f"Unsupported backend '{instance.backend}' for type '{instance.type}'")

    # ------------------------------------------------------------------
    # Active connectors
    # ------------------------------------------------------------------

    def _get_active_connector(
        self, connector_type: str, expected: type[_ConnectorT]
    ) -> _ConnectorT | None:
        """Build the active connector of *connector_type*, or None if unusable.

        Every failure mode (nothing active, unknown id, unbuildable config,
        wrong protocol) is logged and degrades to None so callers can surface a
        single "no connector" message.
        """
        active_id = self.get_active_id_for_type(connector_type)
        if not active_id:
            logger.debug("No active %s connector configured", connector_type)
            return None
        try:
            conn = self.build_connector(self.get_connector(active_id))
        except KeyError:
            logger.warning(
                "Active %s connector %s not found in loaded connectors", connector_type, active_id
            )
            return None
        except ValueError as exc:
            logger.warning(
                "Failed to build active %s connector %s: %s", connector_type, active_id, exc
            )
            return None
        if not isinstance(conn, expected):
            logger.warning(
                "Active %s connector %s is not a %s",
                connector_type,
                active_id,
                expected.__name__,
            )
            return None
        return conn

    def get_active_text_connector(self) -> TextConnector | None:
        # type-abstract: the ABC is used purely as an isinstance/type-narrowing
        # tag here, never instantiated.
        return self._get_active_connector("text", TextConnector)  # type: ignore[type-abstract]

    def get_active_image_connector(self) -> ImageConnector | None:
        return self._get_active_connector("image", ImageConnector)  # type: ignore[type-abstract]

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def list_connectors(self, connector_type: str | None = None) -> list[ConnectorInstance]:
        result = list(self._connectors.values())
        if connector_type is not None:
            result = [c for c in result if c.type == connector_type]
        return result

    def get_connector(self, connector_id: str) -> ConnectorInstance:
        if connector_id not in self._connectors:
            raise KeyError(f"Connector '{connector_id}' not found")
        return self._connectors[connector_id]

    def create_connector(self, data: ConnectorCreate) -> ConnectorInstance:
        now = datetime.now(UTC)
        instance = ConnectorInstance(
            id=str(uuid.uuid4()),
            name=data.name,
            type=data.type,
            backend=data.backend,
            config=data.config,
            created_at=now,
            updated_at=now,
        )
        self._connectors[instance.id] = instance
        self._save_instance(instance)
        return instance

    def update_connector(self, connector_id: str, data: ConnectorUpdate) -> ConnectorInstance:
        existing = self.get_connector(connector_id)
        new_config = dict(data.config)
        # Preserve existing api_key if not explicitly provided in the update body
        if "api_key" not in data.config:
            new_config["api_key"] = existing.config.get("api_key", "")
        updated = ConnectorInstance(
            id=existing.id,
            name=data.name,
            type=data.type,
            backend=data.backend,
            config=new_config,
            created_at=existing.created_at,
            updated_at=datetime.now(UTC),
        )
        self._connectors[connector_id] = updated
        self._save_instance(updated)
        return updated

    def delete_connector(self, connector_id: str) -> None:
        self.get_connector(connector_id)  # raises KeyError if not found
        path = self._dir / f"{connector_id}.json"
        if path.exists():
            path.unlink()
        del self._connectors[connector_id]
        changed = False
        if self._config.active_connectors.text == connector_id:
            self._config.active_connectors.text = ""
            changed = True
        if self._config.active_connectors.image == connector_id:
            self._config.active_connectors.image = ""
            changed = True
        if changed:
            self._save_config()

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    def get_active_id_for_type(self, connector_type: str) -> str:
        """Return the active connector ID for a type, or '' if none is set."""
        if connector_type == "text":
            return self._config.active_connectors.text or ""
        if connector_type == "image":
            return self._config.active_connectors.image or ""
        return ""

    def set_active(self, connector_id: str) -> None:
        instance = self.get_connector(connector_id)
        if instance.type == "text":
            self._config.active_connectors.text = connector_id
        elif instance.type == "image":
            self._config.active_connectors.image = connector_id
        else:
            raise ValueError(f"Activation not supported for connector type '{instance.type}'")
        self._save_config()

    def is_active(self, connector_id: str) -> bool:
        instance = self.get_connector(connector_id)
        if instance.type == "text":
            return self._config.active_connectors.text == connector_id
        if instance.type == "image":
            return self._config.active_connectors.image == connector_id
        return False

    # ------------------------------------------------------------------
    # Test
    # ------------------------------------------------------------------

    async def test_connector(self, connector_id: str) -> dict[str, Any]:
        instance = self.get_connector(connector_id)
        conn = self._build_connector(instance)
        return await conn.test_connection()

    # ------------------------------------------------------------------
    # ComfyUI workflows
    # ------------------------------------------------------------------

    def list_workflows(self) -> list[str]:
        """List all available ComfyUI workflow template names."""
        workflows_dir = self._data_dir / "comfyui_workflows"
        dummy = ComfyUIConnector(ComfyUIConfig(), workflows_dir)
        return dummy.list_all_workflows()
