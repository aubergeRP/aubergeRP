from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from ..models.connector import OpenAIImageConfig
from ..utils.retry import ConnectorHTTPError, retry_async
from .base import ImageConnector

logger = logging.getLogger(__name__)

#: Reference images are sent inline in the JSON body; keep them small.
_REFERENCE_MAX_SIZE = (512, 512)
_REFERENCE_JPEG_QUALITY = 90


def _compress_reference_image(raw: bytes) -> tuple[bytes, str]:
    """Downscale *raw* to fit 512x512, re-encoded as quality-90 JPEG.

    Returns the bytes and their MIME type; on failure the original bytes are
    returned unchanged (as PNG) rather than losing img2img altogether.
    """
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(raw)) as img:
            rgb = img.convert("RGB")
            rgb.thumbnail(_REFERENCE_MAX_SIZE)
            buffer = BytesIO()
            rgb.save(buffer, format="JPEG", quality=_REFERENCE_JPEG_QUALITY)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        logger.warning("Could not compress the reference image; sending it as-is", exc_info=True)
        return raw, "image/png"


class OpenAIImageConnector(ImageConnector):
    backend_id = "openai_api"

    def __init__(self, config: OpenAIImageConfig) -> None:
        self.config = config

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _is_openrouter(self) -> bool:
        return "openrouter.ai" in self.config.base_url.lower()

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

    async def _extract_image_bytes(self, item: dict[str, Any], client: httpx.AsyncClient) -> bytes:
        b64_json = item.get("b64_json")
        if isinstance(b64_json, str) and b64_json:
            return base64.b64decode(b64_json)

        image_url_data = item.get("image_url")
        url = image_url_data.get("url") if isinstance(image_url_data, dict) else item.get("url")

        if not isinstance(url, str) or not url:
            raise ValueError("Image response did not include b64_json or url")

        if url.startswith("data:"):
            _, _, raw = url.partition(",")
            return base64.b64decode(raw)

        img_response = await client.get(url)
        img_response.raise_for_status()
        return img_response.content

    def _format_http_error(self, response: httpx.Response, context: str) -> str:
        """Build a human-readable error from an HTTP error response body."""
        try:
            body = response.json()
            error = body.get("error", {})
            msg = error.get("message", "")
            metadata = error.get("metadata", {})
            raw_str = metadata.get("raw")
            if isinstance(raw_str, str):
                try:
                    raw = json.loads(raw_str)
                    status = raw.get("status", "")
                    reasons = raw.get("details", {}).get("Moderation Reasons", [])
                    if reasons:
                        return f"{context} HTTP {response.status_code}: {msg} — {status}: {', '.join(reasons)}"
                except (json.JSONDecodeError, AttributeError):
                    pass
            if msg:
                return f"{context} HTTP {response.status_code}: {msg}"
        except Exception:
            pass
        return f"{context} HTTP {response.status_code}"

    def _image_data_url(self, reference_image: bytes | None) -> str | None:
        """Encode the reference image as a data URL, when img2img is enabled.

        The portrait is downscaled and re-encoded as JPEG first: a full-size
        PNG data URL easily pushes the request past the provider's body limit
        (HTTP 413).
        """
        if not self.config.image_support or not reference_image:
            return None
        payload, mime = _compress_reference_image(reference_image)
        return f"data:{mime};base64," + base64.b64encode(payload).decode()

    async def _generate_via_openai_images_api(
        self,
        full_prompt: str,
        model: str,
        size: str,
        client: httpx.AsyncClient,
        reference_image: bytes | None = None,
    ) -> bytes:
        payload: dict[str, Any] = dict(self.config.custom_json or {})
        payload.update({
            "model": model,
            "prompt": full_prompt,
            "size": size,
            "n": 1,
        })
        data_url = self._image_data_url(reference_image)
        if data_url:
            payload["imageDataUrl"] = data_url
        logger.debug("[OpenAI Images API] Sending: %s", json.dumps(payload, default=str))
        response = await client.post(
            f"{self.config.base_url}/images/generations",
            headers=self._headers(),
            json=payload,
        )
        if response.status_code >= 400:
            error_msg = self._format_http_error(response, "[OpenAI Images API]")
            logger.error("%s\nPrompt: %s", error_msg, full_prompt[:500])
            raise ConnectorHTTPError(error_msg, response.status_code)
        item = response.json()["data"][0]
        return await self._extract_image_bytes(item, client)

    async def _generate_via_openrouter_chat_api(
        self,
        full_prompt: str,
        model: str,
        size: str,
        client: httpx.AsyncClient,
        reference_image: bytes | None = None,
    ) -> bytes:
        payload: dict[str, Any] = dict(self.config.custom_json or {})
        payload.update({
            "model": model,
            "messages": [{"role": "user", "content": full_prompt}],
            "modalities": ["image"],
            "stream": False,
            "image_config": {"size": size},
        })
        data_url = self._image_data_url(reference_image)
        if data_url:
            payload["imageDataUrl"] = data_url
        logger.debug("[OpenRouter Chat API] Model: %s, Size: %s", model, size)
        logger.debug("[OpenRouter Chat API] Payload: %s", json.dumps(payload, default=str))
        response = await client.post(
            f"{self.config.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        if response.status_code >= 400:
            error_msg = self._format_http_error(response, "[OpenRouter Chat API]")
            logger.error("%s\nPrompt: %s", error_msg, full_prompt[:500])
            raise ConnectorHTTPError(error_msg, response.status_code)

        data = response.json()
        message = (data.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return await self._extract_image_bytes(part, client)

        images = message.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict):
                return await self._extract_image_bytes(first, client)

        raise ValueError("OpenRouter did not return an image in chat/completions response")

    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str = "",
        model: str | None = None,
        size: str | None = None,
        reference_image: bytes | None = None,
    ) -> bytes:
        full_prompt = f"{prompt}. Avoid: {negative_prompt}" if negative_prompt else prompt
        resolved_model = model or self.config.model
        resolved_size = size or self.config.size or "1024x1024"

        async def attempt() -> bytes:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                if self._is_openrouter():
                    return await self._generate_via_openrouter_chat_api(
                        full_prompt,
                        resolved_model,
                        resolved_size,
                        client,
                        reference_image,
                    )
                return await self._generate_via_openai_images_api(
                    full_prompt,
                    resolved_model,
                    resolved_size,
                    client,
                    reference_image,
                )

        return await retry_async(
            attempt, self.config.max_retries, label="Image generation"
        )
