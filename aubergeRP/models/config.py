from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class AppConfigResponse(BaseModel):
    host: str
    port: int
    log_level: LogLevel
    sentry_dsn: str = ""
    admin_token_ttl_seconds: int = 86400
    #: Read-only: changing the data directory at runtime is not supported.
    data_dir: str = "data"


class AppConfigUpdate(BaseModel):
    """Writable subset of the app config.

    ``data_dir`` is deliberately absent: it is reported by the API but can only
    be changed in ``config.yaml``/env before start-up. The admin secrets
    (``admin_password_hash``, ``admin_jwt_secret``) are never exposed at all.
    """

    host: str
    port: int = Field(ge=1, le=65535)
    log_level: LogLevel
    sentry_dsn: str = ""
    admin_token_ttl_seconds: int = Field(default=86400, gt=0)


class ActiveConnectorsResponse(BaseModel):
    text: str = ""
    image: str = ""
    #: Optional per-task text connectors — empty means "same as `text`".
    text_summarization: str = ""
    text_utility: str = ""


class UserConfigResponse(BaseModel):
    name: str


class ChatConfigResponse(BaseModel):
    context_window: int = Field(default=4096, gt=0)
    summarization_threshold: float = Field(default=0.75, gt=0.0, le=1.0)
    ooc_protection: bool = True
    image_autonomy: bool = True
    image_autonomy_cooldown: int = Field(default=4, ge=0)


class SchedulerConfigResponse(BaseModel):
    enabled: bool = False
    interval_seconds: int = Field(default=86400, gt=0)
    cleanup_older_than_days: int = Field(default=30, ge=1)
    health_check_enabled: bool = True
    health_check_interval_seconds: int = Field(default=300, gt=0)


class ObservabilityConfigResponse(BaseModel):
    metrics_enabled: bool = False


class GuiVisibilityResponse(BaseModel):
    """The subset of the GUI config that belongs to the Configuration panel.

    The presentation settings (custom CSS/HTML) stay on ``/api/config/gui``.
    """

    public_character_list: bool = True


class ConfigResponse(BaseModel):
    app: AppConfigResponse
    user: UserConfigResponse
    active_connectors: ActiveConnectorsResponse
    # Defaults mirror the ones in config.py, so the sections can be omitted when
    # building a response by hand (tests) without changing the wire contract:
    # _to_response() always fills them in from the live config.
    chat: ChatConfigResponse = ChatConfigResponse()
    scheduler: SchedulerConfigResponse = SchedulerConfigResponse()
    observability: ObservabilityConfigResponse = ObservabilityConfigResponse()
    gui: GuiVisibilityResponse = GuiVisibilityResponse()


class ConfigUpdate(BaseModel):
    """Partial update — every section is optional, but a provided section is
    replaced as a whole."""

    app: AppConfigUpdate | None = None
    user: UserConfigResponse | None = None
    active_connectors: ActiveConnectorsResponse | None = None
    chat: ChatConfigResponse | None = None
    scheduler: SchedulerConfigResponse | None = None
    observability: ObservabilityConfigResponse | None = None
    gui: GuiVisibilityResponse | None = None


class AppConfigPatch(BaseModel):
    """Per-field optional patch for app config."""
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    log_level: LogLevel | None = None
    sentry_dsn: str | None = None
    admin_token_ttl_seconds: int | None = Field(default=None, gt=0)


class ActiveConnectorsPatch(BaseModel):
    """Per-field optional patch for active connectors."""
    text: str | None = None
    image: str | None = None
    text_summarization: str | None = None
    text_utility: str | None = None


class UserConfigPatch(BaseModel):
    """Per-field optional patch for user config."""
    name: str | None = None


class ChatConfigPatch(BaseModel):
    """Per-field optional patch for chat config."""
    context_window: int | None = Field(default=None, gt=0)
    summarization_threshold: float | None = Field(default=None, gt=0.0, le=1.0)
    ooc_protection: bool | None = None
    image_autonomy: bool | None = None
    image_autonomy_cooldown: int | None = Field(default=None, ge=0)


class SchedulerConfigPatch(BaseModel):
    """Per-field optional patch for scheduler config."""
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, gt=0)
    cleanup_older_than_days: int | None = Field(default=None, ge=1)
    health_check_enabled: bool | None = None
    health_check_interval_seconds: int | None = Field(default=None, gt=0)


class ObservabilityConfigPatch(BaseModel):
    """Per-field optional patch for observability config."""
    metrics_enabled: bool | None = None


class GuiVisibilityPatch(BaseModel):
    """Per-field optional patch for the GUI visibility settings."""
    public_character_list: bool | None = None


class ConfigPatch(BaseModel):
    """Per-field PATCH semantics — only provided fields are updated."""
    app: AppConfigPatch | None = None
    user: UserConfigPatch | None = None
    active_connectors: ActiveConnectorsPatch | None = None
    chat: ChatConfigPatch | None = None
    scheduler: SchedulerConfigPatch | None = None
    observability: ObservabilityConfigPatch | None = None
    gui: GuiVisibilityPatch | None = None


class GuiConfigResponse(BaseModel):
    """GUI customization settings returned by the API."""

    custom_css: str = ""
    custom_header_html: str = ""
    custom_footer_html: str = ""
    public_character_list: bool = True


class GuiConfigUpdate(BaseModel):
    """Full GUI customization update.

    ``public_character_list`` is optional: the Configuration panel owns that
    setting now, so a customization save that omits it must leave it untouched
    instead of silently resetting it to the default.
    """

    custom_css: str = ""
    custom_header_html: str = ""
    custom_footer_html: str = ""
    public_character_list: bool | None = None
