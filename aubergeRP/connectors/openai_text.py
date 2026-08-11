from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..models.connector import OpenAITextConfig
from .base import TextConnector

logger = logging.getLogger(__name__)


class OpenAITextConnector(TextConnector):
    backend_id = "openai_api"

    def __init__(self, config: OpenAITextConfig) -> None:
        self.config = config
        self.supports_tool_calling = config.supports_tool_calling
        # Token usage reported by the provider for the most recent stream, or
        # None when the provider did not report any.  Consumed by the
        # statistics layer so it can distinguish real counts from estimates.
        self.last_usage: dict[str, int] | None = None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        top_k: int | None,
        repeat_penalty: float | None,
        presence_penalty: float | None,
        frequency_penalty: float | None,
        extra_body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the common OpenAI-compatible chat completion payload.

        Caller-supplied values take precedence over connector-level config.
        Optional parameters (top_p, presence_penalty, frequency_penalty,
        extra_body) are omitted from the payload when neither the caller nor
        the connector config specifies them, to avoid sending nulls to APIs
        that reject unknown fields.
        """
        payload: dict[str, Any] = {
            "model": model or self.config.model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
            "temperature": temperature if temperature is not None else self.config.temperature,
        }

        if self.config.stream_usage:
            payload["stream_options"] = {"include_usage": True}

        optional: dict[str, Any] = {
            "top_p": top_p,
            "top_k": top_k,
            "repeat_penalty": repeat_penalty,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
        }
        for name, override in optional.items():
            value = override if override is not None else getattr(self.config, name)
            if value is not None:
                payload[name] = value

        # Merge extra_body: caller overrides connector config.
        extra_body_config = self.config.extra_body or {}
        if extra_body:
            extra_body_config = {**extra_body_config, **extra_body}
        if extra_body_config:
            payload.update(extra_body_config)

        return payload

    async def test_connection(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                response = await client.get(
                    f"{self.config.base_url}/models",
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
                models = [m["id"] for m in data.get("data", [])]

                details: dict[str, Any] = {"models_available": models}

                # If models are available, check if the configured model is in the list
                if models and self.config.model not in models:
                    details["model_warning"] = f"Model '{self.config.model}' not found in available models"

                return {"connected": True, "details": details}
        except Exception as exc:
            return {"connected": False, "details": {"error": str(exc)}}

    def _capture_usage(self, chunk: dict[str, Any]) -> None:
        """Record provider-reported token usage from a streaming chunk.

        Providers that support ``stream_options.include_usage`` emit a final
        chunk carrying a ``usage`` object (and usually no choices).  Providers
        that do not simply never call this, leaving ``last_usage`` as None so
        the caller falls back to its local estimate.
        """
        usage = chunk.get("usage")
        if not isinstance(usage, dict):
            return
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
            return
        self.last_usage = {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
        }

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
        """Stream raw text tokens from a plain chat completion (no tool calls).

        Unlike stream_chat_completion_with_tools, this method yields plain
        strings (one per SSE token chunk) and also tracks reasoning-only
        content so the caller can detect empty responses from thinking models.
        Tools are not sent; the LLM expresses image generation through the
        [IMG:…] text marker convention instead.
        """
        payload = self._build_payload(
            messages, model, temperature, max_tokens,
            top_p, top_k, repeat_penalty, presence_penalty, frequency_penalty, extra_body,
        )

        model_name = payload["model"]
        req_bytes = len(json.dumps(payload).encode())
        logger.info("LLM request: model=%s messages=%d req=%d bytes", model_name, len(messages), req_bytes)
        logger.debug("LLM payload: %s", json.dumps(payload, ensure_ascii=False))

        total_chars = 0
        total_ignored_chars = 0
        total_reasoning_chars = 0
        self.last_usage = None
        async with httpx.AsyncClient(timeout=self.config.timeout) as client, client.stream(
            "POST",
            f"{self.config.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():  # Skip blank lines (keep-alive)
                    continue
                if not line.startswith("data: "):
                    continue

                payload_str = line[6:]
                if payload_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(payload_str)
                    self._capture_usage(chunk)
                    delta = chunk["choices"][0]["delta"]
                    content = delta.get("content")
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if content:
                        total_chars += len(content)
                        yield content
                    else:
                        total_ignored_chars += len(payload_str)
                    if reasoning:
                        total_reasoning_chars += len(reasoning)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
        if total_chars == 0 and total_reasoning_chars > 0:
            logger.warning(
                "LLM response: model=%s — response content is empty but %d reasoning chars "
                "were detected. The model placed its output in the reasoning/thinking section "
                "instead of the final message. Consider: 1) using a system prompt that "
                "discourages reasoning for roleplay output, 2) raising the max_tokens limit "
                "to accommodate reasoning tokens.",
                model_name,
                total_reasoning_chars,
            )
        logger.info(
            "LLM response: model=%s resp=%d chars reasoning=%d chars ignored=%d chars",
            model_name, total_chars, total_reasoning_chars, total_ignored_chars
        )

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
        """Stream a chat completion that may produce text tokens and/or tool calls.

        Unlike stream_chat_completion, this method sends the tools list and
        tool_choice to the API and yields typed event dicts instead of raw
        strings.  Tool-call argument fragments are accumulated across chunks
        and emitted as complete events after the stream closes.

        Yields:
          {"type": "token", "content": str}
          {"type": "tool_call", "name": str, "arguments": dict}
        """
        payload = self._build_payload(
            messages, model, temperature, max_tokens,
            top_p, top_k, repeat_penalty, presence_penalty, frequency_penalty, extra_body,
        )
        # Extend with tool-calling fields absent from the plain completion path.
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

        model_name = payload["model"]
        req_bytes = len(json.dumps(payload).encode())
        logger.info(
            "LLM request (tools): model=%s messages=%d tools=%d req=%d bytes",
            model_name,
            len(messages),
            len(tools),
            req_bytes
        )
        logger.debug("LLM payload: %s", json.dumps(payload, ensure_ascii=False))

        # Accumulate tool-call argument fragments keyed by call index.
        tool_calls: dict[int, dict[str, Any]] = {}
        total_chars = 0
        self.last_usage = None

        async with httpx.AsyncClient(timeout=self.config.timeout) as client, client.stream(
            "POST",
            f"{self.config.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:]
                if payload_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload_str)
                    self._capture_usage(chunk)
                    delta = chunk["choices"][0]["delta"]

                    # Text content
                    content = delta.get("content")
                    if content:
                        total_chars += len(content)
                        yield {"type": "token", "content": content}

                    # Tool call fragments
                    for tc_delta in delta.get("tool_calls") or []:
                        idx: int = tc_delta["index"]
                        if idx not in tool_calls:
                            tool_calls[idx] = {
                                "id": tc_delta.get("id", ""),
                                "name": tc_delta.get("function", {}).get("name", ""),
                                "arguments_raw": "",
                            }
                        fn = tc_delta.get("function", {})
                        if fn.get("name"):
                            tool_calls[idx]["name"] = fn["name"]
                        if fn.get("arguments"):
                            tool_calls[idx]["arguments_raw"] += fn["arguments"]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        logger.info("LLM response (tools): model=%s resp=%d chars tool_calls=%d", model_name, total_chars, len(tool_calls))

        # Emit completed tool calls in order.
        for idx in sorted(tool_calls):
            tc = tool_calls[idx]
            try:
                arguments = json.loads(tc["arguments_raw"]) if tc["arguments_raw"] else {}
            except json.JSONDecodeError:
                arguments = {"raw": tc["arguments_raw"]}
            yield {"type": "tool_call", "name": tc["name"], "arguments": arguments}
