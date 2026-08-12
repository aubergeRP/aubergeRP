from __future__ import annotations

import logging
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException

from ..config import _strip_js, get_config
from ..models.config import (
    ActiveConnectorsResponse,
    AppConfigResponse,
    ConfigPatch,
    ConfigResponse,
    ConfigUpdate,
    GuiConfigResponse,
    GuiConfigUpdate,
    UserConfigResponse,
)
from .admin import get_admin_token
from .connectors import get_connector_manager
from .errors import config_write_error

router = APIRouter(prefix="/config", tags=["config"])


def get_config_save_path() -> Path:
    return Path("config.yaml")


def _to_response() -> ConfigResponse:
    config = get_config()
    return ConfigResponse(
        app=AppConfigResponse(
            host=config.app.host,
            port=config.app.port,
            log_level=config.app.log_level,
        ),
        user=UserConfigResponse(name=config.user.name),
        active_connectors=ActiveConnectorsResponse(
            text=config.active_connectors.text,
            image=config.active_connectors.image,
            text_summarization=config.active_connectors.text_summarization,
            text_utility=config.active_connectors.text_utility,
        ),
    )


def _validate_text_override(connector_id: str) -> str:
    """Ensure a per-task override points at an existing *text* connector.

    An empty string is always valid: it means "same as the main text model".
    The connector manager is resolved lazily so that requests which do not set
    an override never instantiate it.
    """
    if not connector_id:
        return ""
    try:
        instance = get_connector_manager().get_connector(connector_id)
    except KeyError:
        raise HTTPException(
            status_code=400, detail=f"Unknown connector '{connector_id}'"
        ) from None
    if instance.type != "text":
        raise HTTPException(
            status_code=400, detail=f"Connector '{connector_id}' is not a text connector"
        )
    return connector_id


def _save_config(save_path: Path) -> None:
    config = get_config()
    data = config.model_dump()
    try:
        with save_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    except OSError as exc:
        raise config_write_error(exc) from None


@router.get("/")
def get_config_endpoint() -> ConfigResponse:
    return _to_response()


@router.put("/")
def update_config(
    update: ConfigUpdate,
    save_path: Path = Depends(get_config_save_path),
    admin_token: str = Depends(get_admin_token),
) -> ConfigResponse:
    config = get_config()

    if update.app is not None:
        config.app.host = update.app.host
        config.app.port = update.app.port
        config.app.log_level = update.app.log_level
        logging.getLogger().setLevel(getattr(logging, update.app.log_level, logging.INFO))

    if update.user is not None:
        config.user.name = update.user.name

    if update.active_connectors is not None:
        config.active_connectors.text = update.active_connectors.text
        config.active_connectors.image = update.active_connectors.image
        config.active_connectors.text_summarization = _validate_text_override(
            update.active_connectors.text_summarization
        )
        config.active_connectors.text_utility = _validate_text_override(
            update.active_connectors.text_utility
        )

    _save_config(save_path)
    return _to_response()


@router.patch("/")
def patch_config(
    patch: ConfigPatch,
    save_path: Path = Depends(get_config_save_path),
    admin_token: str = Depends(get_admin_token),
) -> ConfigResponse:
    config = get_config()

    if patch.app is not None:
        if patch.app.host is not None:
            config.app.host = patch.app.host
        if patch.app.port is not None:
            config.app.port = patch.app.port
        if patch.app.log_level is not None:
            config.app.log_level = patch.app.log_level
            logging.getLogger().setLevel(getattr(logging, patch.app.log_level, logging.INFO))

    if patch.user is not None and patch.user.name is not None:
        config.user.name = patch.user.name

    if patch.active_connectors is not None:
        if patch.active_connectors.text is not None:
            config.active_connectors.text = patch.active_connectors.text
        if patch.active_connectors.image is not None:
            config.active_connectors.image = patch.active_connectors.image
        if patch.active_connectors.text_summarization is not None:
            config.active_connectors.text_summarization = _validate_text_override(
                patch.active_connectors.text_summarization
            )
        if patch.active_connectors.text_utility is not None:
            config.active_connectors.text_utility = _validate_text_override(
                patch.active_connectors.text_utility
            )

    _save_config(save_path)
    return _to_response()


# ── GUI Customization ─────────────────────────────────────────────────────────

@router.get("/gui", response_model=GuiConfigResponse)
def get_gui_config() -> GuiConfigResponse:
    config = get_config()
    return GuiConfigResponse(
        custom_css=config.gui.custom_css,
        custom_header_html=config.gui.custom_header_html,
        custom_footer_html=config.gui.custom_footer_html,
        public_character_list=config.gui.public_character_list,
    )


@router.put("/gui", response_model=GuiConfigResponse)
def update_gui_config(
    update: GuiConfigUpdate,
    save_path: Path = Depends(get_config_save_path),
    admin_token: str = Depends(get_admin_token),
) -> GuiConfigResponse:
    config = get_config()
    config.gui.custom_css = update.custom_css
    config.gui.custom_header_html = _strip_js(update.custom_header_html)
    config.gui.custom_footer_html = _strip_js(update.custom_footer_html)
    config.gui.public_character_list = update.public_character_list
    _save_config(save_path)
    return GuiConfigResponse(
        custom_css=config.gui.custom_css,
        custom_header_html=config.gui.custom_header_html,
        custom_footer_html=config.gui.custom_footer_html,
        public_character_list=config.gui.public_character_list,
    )
