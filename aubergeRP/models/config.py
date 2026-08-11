from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AppConfigResponse(BaseModel):
    host: str
    port: int
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ActiveConnectorsResponse(BaseModel):
    text: str = ""
    image: str = ""
    #: Optional per-task text connectors — empty means "same as `text`".
    text_summarization: str = ""
    text_utility: str = ""


class UserConfigResponse(BaseModel):
    name: str


class ConfigResponse(BaseModel):
    app: AppConfigResponse
    user: UserConfigResponse
    active_connectors: ActiveConnectorsResponse


class ConfigUpdate(BaseModel):
    """Partial update — all fields optional."""
    app: AppConfigResponse | None = None
    user: UserConfigResponse | None = None
    active_connectors: ActiveConnectorsResponse | None = None


class AppConfigPatch(BaseModel):
    """Per-field optional patch for app config."""
    host: str | None = None
    port: int | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] | None = None


class ActiveConnectorsPatch(BaseModel):
    """Per-field optional patch for active connectors."""
    text: str | None = None
    image: str | None = None
    text_summarization: str | None = None
    text_utility: str | None = None


class UserConfigPatch(BaseModel):
    """Per-field optional patch for user config."""
    name: str | None = None


class ConfigPatch(BaseModel):
    """Per-field PATCH semantics — only provided fields are updated."""
    app: AppConfigPatch | None = None
    user: UserConfigPatch | None = None
    active_connectors: ActiveConnectorsPatch | None = None


class GuiConfigResponse(BaseModel):
    """GUI customization settings returned by the API."""

    custom_css: str = ""
    custom_header_html: str = ""
    custom_footer_html: str = ""


class GuiConfigUpdate(BaseModel):
    """Full GUI customization update."""

    custom_css: str = ""
    custom_header_html: str = ""
    custom_footer_html: str = ""
