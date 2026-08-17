from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ConnectorType = Literal["text", "image", "video", "audio"]
ConnectorBackend = Literal["openai_api", "comfyui"]


class CustomJSONConfig(BaseModel):
    """Base for connector configs exposing a free-form ``custom_json`` field.

    The dict is merged at the root of the outgoing request payload with the
    LOWEST priority: every value computed by aubergeRP overrides it.
    """

    custom_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("custom_json", mode="before")
    @classmethod
    def coerce_custom_json(cls, v: Any) -> Any:
        if v == "" or v is None:
            return {}
        return v


class OpenAITextConfig(CustomJSONConfig):
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "llama3"
    max_tokens: int = 1024
    context_window: int = 4096
    temperature: float = 0.8
    top_p: float | None = None
    top_k: int | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    timeout: int = 120
    max_retries: int = Field(default=3, ge=0, le=10)
    supports_tool_calling: bool = True
    # Ask the provider to report real token usage at the end of a stream
    # (OpenAI's ``stream_options.include_usage``).  Disable it for providers
    # that reject the field; token counts then fall back to a local estimate.
    stream_usage: bool = True

    @field_validator("extra_body", mode="before")
    @classmethod
    def coerce_extra_body(cls, v: Any) -> Any:
        if v == "" or v is None:
            return {}
        return v


class OpenAIImageConfig(CustomJSONConfig):
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""
    model: str = "google/gemini-2.0-flash-exp:free"
    size: str = "1024x1024"
    # When enabled, the character portrait is sent alongside the prompt as an
    # ``imageDataUrl`` data URL so the backend can do img2img.
    image_support: bool = False
    timeout: int = 120
    max_retries: int = Field(default=3, ge=0, le=10)


class ComfyUIConfig(CustomJSONConfig):
    base_url: str = "http://localhost:8188"
    workflow: str = "default"
    timeout: int = 300
    max_retries: int = Field(default=3, ge=0, le=10)


class ConnectorInstance(BaseModel):
    """Stored on disk — includes api_key."""
    id: str
    name: str
    type: ConnectorType
    backend: ConnectorBackend
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ConnectorResponse(BaseModel):
    """API response — api_key is redacted to api_key_set."""
    id: str
    name: str
    type: ConnectorType
    backend: ConnectorBackend
    is_active: bool
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ConnectorCreate(BaseModel):
    name: str = Field(..., min_length=1)
    type: ConnectorType
    backend: ConnectorBackend
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdate(BaseModel):
    name: str = Field(..., min_length=1)
    type: ConnectorType
    backend: ConnectorBackend
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorTestResult(BaseModel):
    connected: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ConnectorActivateResult(BaseModel):
    id: str
    type: ConnectorType
    is_active: bool
