import base64
import json

import httpx
import pytest
import respx

from aubergeRP.connectors.openai_image import OpenAIImageConnector
from aubergeRP.models.connector import OpenAIImageConfig

BASE_OPENROUTER = "https://openrouter.ai/api/v1"
BASE_OPENAI_COMPAT = "https://api.example.com/v1"
IMAGE_URL = "https://cdn.example.com/img/abc.png"
FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"


def make_connector(**overrides) -> OpenAIImageConnector:
    defaults = dict(base_url=BASE_OPENROUTER, api_key="sk-or-test")
    defaults.update(overrides)
    return OpenAIImageConnector(OpenAIImageConfig(**defaults))


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@respx.mock
async def test_connection_success():
    respx.get(f"{BASE_OPENROUTER}/models").respond(
        200,
        json={
            "data": [
                {"id": "google/gemini-2.0-flash-exp:free"},
                {"id": "dall-e-3"},
            ]
        },
    )
    result = await make_connector().test_connection()
    assert result["connected"] is True
    assert "google/gemini-2.0-flash-exp:free" in result["details"]["models_available"]
    assert "model_warning" not in result["details"]


@respx.mock
async def test_connection_model_not_found():
    """Test when configured model is not in the available models list."""
    respx.get(f"{BASE_OPENROUTER}/models").respond(
        200, json={"data": [{"id": "gemini-flash"}, {"id": "dall-e-3"}]}
    )
    result = await make_connector(model="gpt-4").test_connection()
    assert result["connected"] is True
    assert "gemini-flash" in result["details"]["models_available"]
    assert "model_warning" in result["details"]
    assert "gpt-4" in result["details"]["model_warning"]


@respx.mock
async def test_connection_no_models_available_is_ok():
    """Test when no models are available - be kind and don't warn."""
    respx.get(f"{BASE_OPENROUTER}/models").respond(
        200, json={"data": []}
    )
    result = await make_connector(model="unknown-model").test_connection()
    assert result["connected"] is True
    assert result["details"]["models_available"] == []
    assert "model_warning" not in result["details"]


@respx.mock
async def test_connection_auth_error():
    respx.get(f"{BASE_OPENROUTER}/models").respond(401)
    result = await make_connector().test_connection()
    assert result["connected"] is False


@respx.mock
async def test_connection_network_error():
    respx.get(f"{BASE_OPENROUTER}/models").mock(side_effect=httpx.ConnectError("no route"))
    result = await make_connector().test_connection()
    assert result["connected"] is False


# ---------------------------------------------------------------------------
# generate_image (OpenRouter) — /chat/completions path
# ---------------------------------------------------------------------------


@respx.mock
async def test_generate_image_openrouter_data_url_response():
    data_url = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            }
                        ]
                    }
                }
            ]
        },
    )
    result = await make_connector().generate_image("a red apple")
    assert result == FAKE_PNG


@respx.mock
async def test_generate_image_openrouter_remote_url_response():
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": IMAGE_URL},
                            }
                        ]
                    }
                }
            ]
        },
    )
    respx.get(IMAGE_URL).respond(200, content=FAKE_PNG)
    result = await make_connector().generate_image("a blue sky")
    assert result == FAKE_PNG


@respx.mock
async def test_generate_image_openrouter_negative_prompt_and_config():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode(),
                                },
                            }
                        ]
                    }
                }
            ]
        },
    )
    await make_connector().generate_image("an elf", negative_prompt="blurry, deformed")
    body = json.loads(route.calls[0].request.content)
    assert body["messages"][0]["content"] == "an elf. Avoid: blurry, deformed"
    assert body["modalities"] == ["image"]
    assert body["image_config"]["size"] == "1024x1024"


@respx.mock
async def test_generate_image_openrouter_uses_overrides():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode(),
                                },
                            }
                        ]
                    }
                }
            ]
        },
    )
    await make_connector().generate_image("test", model="openai/gpt-image-1", size="768x768")
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "openai/gpt-image-1"
    assert body["image_config"]["size"] == "768x768"


@respx.mock
async def test_generate_image_openrouter_raises_on_error():
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(502)
    with pytest.raises(ValueError, match="HTTP 502"):
        await make_connector().generate_image("test")


@respx.mock
async def test_generate_image_retries_transient_error_then_succeeds():
    """A 503 is transient: the request is retried and the image comes through."""
    data_url = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"images": [{"image_url": {"url": data_url}}]}}
                    ]
                },
            ),
        ]
    )
    result = await make_connector(max_retries=2).generate_image("a red apple")
    assert result == FAKE_PNG
    assert route.call_count == 2


@respx.mock
async def test_generate_image_does_not_retry_definitive_error():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(400)
    with pytest.raises(ValueError, match="HTTP 400"):
        await make_connector(max_retries=3).generate_image("test")
    assert route.call_count == 1


@respx.mock
async def test_generate_image_gives_up_after_max_retries():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(503)
    with pytest.raises(ValueError, match="HTTP 503"):
        await make_connector(max_retries=2).generate_image("test")
    assert route.call_count == 3


@respx.mock
async def test_generate_image_openrouter_400_clean_message():
    """HTTP 400 with JSON error body raises ValueError with clean message (no MDN URL)."""
    error_body = {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
                "raw": json.dumps({
                    "status": "Request Moderated",
                    "details": {"Moderation Reasons": ["Content Policy Violation"]},
                }),
                "provider_name": "Black Forest Labs",
            },
        }
    }
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(400, json=error_body)
    with pytest.raises(ValueError) as exc_info:
        await make_connector().generate_image("explicit scene")
    msg = str(exc_info.value)
    assert "HTTP 400" in msg
    assert "Content Policy Violation" in msg
    assert "developer.mozilla.org" not in msg


@respx.mock
async def test_generate_image_openrouter_400_json_error_no_metadata():
    """HTTP 400 with simple JSON error message (no nested metadata)."""
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        400, json={"error": {"message": "Invalid model", "code": 400}}
    )
    with pytest.raises(ValueError) as exc_info:
        await make_connector().generate_image("test")
    msg = str(exc_info.value)
    assert "HTTP 400" in msg
    assert "Invalid model" in msg
    assert "developer.mozilla.org" not in msg


@respx.mock
async def test_generate_image_openrouter_400_logs_prompt(caplog):
    """Prompt is included in the ERROR log when generation fails."""
    import logging
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        400, json={"error": {"message": "Provider returned error", "code": 400}}
    )
    with caplog.at_level(logging.ERROR, logger="aubergeRP.connectors.openai_image"), pytest.raises(ValueError):
        await make_connector().generate_image("a dragon in a forest")
    assert any("a dragon in a forest" in r.message for r in caplog.records)


@respx.mock
async def test_generate_image_openrouter_raises_if_no_image():
    respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={"choices": [{"message": {"content": [{"type": "text", "text": "no image"}]}}]},
    )
    with pytest.raises(ValueError, match="did not return an image"):
        await make_connector().generate_image("test")


@respx.mock
async def test_generate_image_openrouter_sends_auth_header():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode(),
                                },
                            }
                        ]
                    }
                }
            ]
        },
    )
    await make_connector(api_key="sk-or-secret").generate_image("test")
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-or-secret"


# ---------------------------------------------------------------------------
# generate_image (OpenAI-compatible) — /images/generations path
def test_headers_omits_authorization_when_api_key_empty():
    headers = make_connector(api_key="")._headers()
    assert "Authorization" not in headers


def test_headers_includes_authorization_when_api_key_set():
    headers = make_connector(api_key="sk-test")._headers()
    assert headers["Authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------


@respx.mock
async def test_generate_image_openai_compat_b64_json():
    b64 = base64.b64encode(FAKE_PNG).decode()
    respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": b64}]}
    )
    result = await make_connector(base_url=BASE_OPENAI_COMPAT).generate_image("a red apple")
    assert result == FAKE_PNG


@respx.mock
async def test_generate_image_openai_compat_url_fallback():
    respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"url": IMAGE_URL}]}
    )
    respx.get(IMAGE_URL).respond(200, content=FAKE_PNG)
    result = await make_connector(base_url=BASE_OPENAI_COMPAT).generate_image("a blue sky")
    assert result == FAKE_PNG


@respx.mock
async def test_generate_image_openai_compat_400_clean_message():
    """OpenAI Images API HTTP 400 raises ValueError with clean message (no MDN URL)."""
    respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        400, json={"error": {"message": "Billing quota exceeded", "code": "billing_hard_limit_reached"}}
    )
    with pytest.raises(ValueError) as exc_info:
        await make_connector(base_url=BASE_OPENAI_COMPAT).generate_image("test")
    msg = str(exc_info.value)
    assert "HTTP 400" in msg
    assert "Billing quota exceeded" in msg
    assert "developer.mozilla.org" not in msg


@respx.mock
async def test_generate_image_openai_compat_400_logs_prompt(caplog):
    """Prompt is included in ERROR log for OpenAI Images API failures."""
    import logging
    respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        400, json={"error": {"message": "Bad request", "code": 400}}
    )
    with caplog.at_level(logging.ERROR, logger="aubergeRP.connectors.openai_image"), pytest.raises(ValueError):
        await make_connector(base_url=BASE_OPENAI_COMPAT).generate_image("medieval castle at night")
    assert any("medieval castle at night" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# size fallback
# ---------------------------------------------------------------------------


@respx.mock
async def test_generate_image_openai_compat_empty_size_falls_back_to_default():
    """Empty size string in config falls back to 1024x1024, not sent as empty."""
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    await make_connector(base_url=BASE_OPENAI_COMPAT, size="").generate_image("a castle")
    body = json.loads(route.calls[0].request.content)
    assert body["size"] == "1024x1024"


@respx.mock
async def test_generate_image_openai_compat_none_size_falls_back_to_default():
    """None size param with empty config falls back to 1024x1024."""
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    await make_connector(base_url=BASE_OPENAI_COMPAT, size="").generate_image("a castle", size=None)
    body = json.loads(route.calls[0].request.content)
    assert body["size"] == "1024x1024"


@respx.mock
async def test_generate_image_openai_compat_explicit_size_overrides_default():
    """Explicit size param is sent as-is and not overridden."""
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    await make_connector(base_url=BASE_OPENAI_COMPAT).generate_image("a castle", size="512x512")
    body = json.loads(route.calls[0].request.content)
    assert body["size"] == "512x512"


# ---------------------------------------------------------------------------
# _format_http_error helper
# ---------------------------------------------------------------------------

def test_format_http_error_moderation():
    """Moderation reasons are surfaced in the formatted message."""
    connector = make_connector()
    raw = json.dumps({
        "status": "Request Moderated",
        "details": {"Moderation Reasons": ["Content Policy Violation", "Adult Content"]},
    })
    mock_resp = httpx.Response(400, json={"error": {"message": "Provider returned error", "metadata": {"raw": raw}}})
    msg = connector._format_http_error(mock_resp, "[Test]")
    assert "HTTP 400" in msg
    assert "Content Policy Violation" in msg
    assert "Adult Content" in msg
    assert "Request Moderated" in msg


def test_format_http_error_simple_message():
    """Simple JSON error message is returned cleanly."""
    connector = make_connector()
    mock_resp = httpx.Response(429, json={"error": {"message": "Rate limit exceeded"}})
    msg = connector._format_http_error(mock_resp, "[Test]")
    assert "HTTP 429" in msg
    assert "Rate limit exceeded" in msg


def test_format_http_error_non_json_body():
    """Non-JSON body falls back to just the status code."""
    connector = make_connector()
    mock_resp = httpx.Response(503, text="Service Unavailable")
    msg = connector._format_http_error(mock_resp, "[Test]")
    assert "HTTP 503" in msg


# ---------------------------------------------------------------------------
# custom_json
# ---------------------------------------------------------------------------


@respx.mock
async def test_custom_json_merged_openai_images_api():
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    conn = make_connector(
        base_url=BASE_OPENAI_COMPAT,
        model="dall-e-3",
        custom_json={"quality": "hd", "model": "ignored", "n": 5},
    )
    await conn.generate_image("a red apple")
    body = json.loads(route.calls[0].request.content)
    assert body["quality"] == "hd"
    # Computed values win over custom_json.
    assert body["model"] == "dall-e-3"
    assert body["n"] == 1


@respx.mock
async def test_custom_json_merged_openrouter_chat_api():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={
            "choices": [
                {"message": {"images": [{"image_url": {"url": IMAGE_URL}}]}}
            ]
        },
    )
    respx.get(IMAGE_URL).respond(200, content=FAKE_PNG)
    conn = make_connector(custom_json={"provider": {"order": ["x"]}, "stream": True})
    await conn.generate_image("a blue sky")
    body = json.loads(route.calls[0].request.content)
    assert body["provider"] == {"order": ["x"]}
    # Computed values win over custom_json.
    assert body["stream"] is False


def test_custom_json_empty_string_coerced():
    assert make_connector(custom_json="").config.custom_json == {}


# ---------------------------------------------------------------------------
# image_support (img2img reference portrait)
# ---------------------------------------------------------------------------


@respx.mock
async def test_reference_image_ignored_when_image_support_disabled():
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    conn = make_connector(base_url=BASE_OPENAI_COMPAT)
    await conn.generate_image("a red apple", reference_image=FAKE_PNG)
    body = json.loads(route.calls[0].request.content)
    assert "imageDataUrl" not in body


@respx.mock
async def test_reference_image_sent_openai_images_api():
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    conn = make_connector(base_url=BASE_OPENAI_COMPAT, image_support=True)
    await conn.generate_image("a red apple", reference_image=FAKE_PNG)
    body = json.loads(route.calls[0].request.content)
    assert body["imageDataUrl"] == (
        "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
    )


@respx.mock
async def test_reference_image_sent_openrouter_chat_api():
    route = respx.post(f"{BASE_OPENROUTER}/chat/completions").respond(
        200,
        json={"choices": [{"message": {"images": [{"image_url": {"url": IMAGE_URL}}]}}]},
    )
    respx.get(IMAGE_URL).respond(200, content=FAKE_PNG)
    conn = make_connector(image_support=True)
    await conn.generate_image("a blue sky", reference_image=FAKE_PNG)
    body = json.loads(route.calls[0].request.content)
    assert body["imageDataUrl"].startswith("data:image/png;base64,")


@respx.mock
async def test_no_reference_image_sends_nothing_even_when_enabled():
    route = respx.post(f"{BASE_OPENAI_COMPAT}/images/generations").respond(
        200, json={"data": [{"b64_json": base64.b64encode(FAKE_PNG).decode()}]}
    )
    conn = make_connector(base_url=BASE_OPENAI_COMPAT, image_support=True)
    await conn.generate_image("a red apple")
    body = json.loads(route.calls[0].request.content)
    assert "imageDataUrl" not in body


def test_image_support_defaults_to_false():
    assert make_connector().config.image_support is False
