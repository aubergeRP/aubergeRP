from __future__ import annotations

import logging
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException

from ..config import ChatConfig, ObservabilityConfig, SchedulerConfig, _strip_js, get_config
from ..models.config import (
    ActiveConnectorsResponse,
    AppConfigResponse,
    ChatConfigResponse,
    ConfigPatch,
    ConfigResponse,
    ConfigUpdate,
    GuiConfigResponse,
    GuiConfigUpdate,
    GuiVisibilityResponse,
    ObservabilityConfigResponse,
    SchedulerConfigResponse,
    UserConfigResponse,
)
from .admin import get_admin_token
from .connectors import get_connector_manager
from .errors import config_write_error

router = APIRouter(prefix="/config", tags=["config"])


def get_config_save_path() -> Path:
    return Path("config.yaml")


def _to_response() -> ConfigResponse:
    """Project the live config onto the API shape.

    ``app.admin_password_hash`` and ``app.admin_jwt_secret`` are intentionally
    never included: they are secrets and are only settable via config.yaml/env.
    """
    config = get_config()
    return ConfigResponse(
        app=AppConfigResponse(
            host=config.app.host,
            port=config.app.port,
            log_level=config.app.log_level,
            sentry_dsn=config.app.sentry_dsn,
            admin_token_ttl_seconds=config.app.admin_token_ttl_seconds,
            data_dir=config.app.data_dir,
        ),
        user=UserConfigResponse(name=config.user.name),
        active_connectors=ActiveConnectorsResponse(
            text=config.active_connectors.text,
            image=config.active_connectors.image,
            text_summarization=config.active_connectors.text_summarization,
            text_utility=config.active_connectors.text_utility,
        ),
        chat=ChatConfigResponse(**config.chat.model_dump()),
        scheduler=SchedulerConfigResponse(**config.scheduler.model_dump()),
        observability=ObservabilityConfigResponse(**config.observability.model_dump()),
        gui=GuiVisibilityResponse(public_character_list=config.gui.public_character_list),
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
        config.app.sentry_dsn = update.app.sentry_dsn
        config.app.admin_token_ttl_seconds = update.app.admin_token_ttl_seconds
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

    if update.chat is not None:
        config.chat = ChatConfig(**update.chat.model_dump())

    if update.scheduler is not None:
        config.scheduler = SchedulerConfig(**update.scheduler.model_dump())

    if update.observability is not None:
        config.observability = ObservabilityConfig(**update.observability.model_dump())

    if update.gui is not None:
        config.gui.public_character_list = update.gui.public_character_list

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
        if patch.app.sentry_dsn is not None:
            config.app.sentry_dsn = patch.app.sentry_dsn
        if patch.app.admin_token_ttl_seconds is not None:
            config.app.admin_token_ttl_seconds = patch.app.admin_token_ttl_seconds

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

    # The remaining sections are plain scalars with no side effects, so a
    # generic "apply the fields that were provided" pass is enough.
    for section in ("chat", "scheduler", "observability", "gui"):
        section_patch = getattr(patch, section)
        if section_patch is None:
            continue
        target = getattr(config, section)
        for field, value in section_patch.model_dump(exclude_none=True).items():
            setattr(target, field, value)

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
    # Owned by the Configuration panel: only touch it when explicitly provided,
    # so saving custom CSS here cannot silently re-publish the character list.
    if update.public_character_list is not None:
        config.gui.public_character_list = update.public_character_list
    _save_config(save_path)
    return GuiConfigResponse(
        custom_css=config.gui.custom_css,
        custom_header_html=config.gui.custom_header_html,
        custom_footer_html=config.gui.custom_footer_html,
        public_character_list=config.gui.public_character_list,
    )
