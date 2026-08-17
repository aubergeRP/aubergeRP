from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class BaseConnector(ABC):
    connector_type: str
    backend_id: str

    @property
    def max_retries(self) -> int:
        """How many times a failed request is retried, from the connector config."""
        return int(getattr(getattr(self, "config", None), "max_retries", 0))

    @abstractmethod
    async def test_connection(self) -> dict[str, Any]:
        """Return {'connected': bool, 'details': {...}}."""


class TextConnector(BaseConnector):
    connector_type = "text"

    #: Subclasses override this to True when the backend supports OpenAI-style
    #: function / tool calling.
    supports_tool_calling: bool = False

    @abstractmethod
    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Yield text tokens from a streaming chat completion."""
        # Concrete implementations are async generators (async def … yield …).
        # The abstract signature uses AsyncIterator so subclasses that return
        # AsyncIterator satisfy the override without a type error.
        raise NotImplementedError  # pragma: no cover
        yield  # make the base a generator stub so subclasses can use `yield`

    async def stream_chat_completion_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events from a streaming chat completion that may include tool calls.

        Events:
          {"type": "token", "content": str}          — a text delta
          {"type": "tool_call", "name": str, "arguments": dict}  — a completed tool call

        The default implementation falls back to plain streaming (no tool calls).
        Connectors that support tool calling override this method.
        """
        async for chunk in self.stream_chat_completion(
            messages, model, temperature, max_tokens, top_p, top_k, repeat_penalty,
            presence_penalty, frequency_penalty, extra_body
        ):
            yield {"type": "token", "content": chunk}


class ImageConnector(BaseConnector):
    connector_type = "image"

    @abstractmethod
    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
    ) -> bytes:
        """Generate an image and return raw PNG bytes."""

    async def generate_image_with_progress(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield progress and completion events during image generation.

        Events:
          {"type": "progress", "step": int, "total": int}
          {"type": "complete", "bytes": bytes}

        Default implementation wraps generate_image() with a single complete event.
        Subclasses can override to emit granular progress events.
        """
        img_bytes = await self.generate_image(prompt, negative_prompt, model, size)
        yield {"type": "complete", "bytes": img_bytes}
