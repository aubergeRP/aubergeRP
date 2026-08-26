import json

import httpx
import pytest
import respx

from aubergeRP.connectors.openai_text import OpenAITextConnector
from aubergeRP.models.connector import OpenAITextConfig

BASE_URL = "http://localhost:11434/v1"


def make_connector(**overrides) -> OpenAITextConnector:
    defaults = dict(base_url=BASE_URL, api_key="test-key", model="llama3")
    defaults.update(overrides)
    return OpenAITextConnector(OpenAITextConfig(**defaults))


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@respx.mock
async def test_connection_success():
    respx.get(f"{BASE_URL}/models").respond(
        200,
        json={"data": [{"id": "llama3"}, {"id": "mistral"}]},
    )
    result = await make_connector().test_connection()
    assert result["connected"] is True
    assert "llama3" in result["details"]["models_available"]
    assert "mistral" in result["details"]["models_available"]
    assert "model_warning" not in result["details"]


@respx.mock
async def test_connection_model_not_found():
    """Test when configured model is not in the available models list."""
    respx.get(f"{BASE_URL}/models").respond(
        200,
        json={"data": [{"id": "llama3"}, {"id": "mistral"}]},
    )
    result = await make_connector(model="qwen-27b").test_connection()
    assert result["connected"] is True
    assert "llama3" in result["details"]["models_available"]
    assert "model_warning" in result["details"]
    assert "qwen-27b" in result["details"]["model_warning"]


@respx.mock
async def test_connection_no_models_available_is_ok():
    """Test when no models are available - be kind and don't warn."""
    respx.get(f"{BASE_URL}/models").respond(
        200,
        json={"data": []},
    )
    result = await make_connector(model="unknown-model").test_connection()
    assert result["connected"] is True
    assert result["details"]["models_available"] == []
    assert "model_warning" not in result["details"]


@respx.mock
async def test_connection_http_error():
    respx.get(f"{BASE_URL}/models").respond(401)
    result = await make_connector().test_connection()
    assert result["connected"] is False
    assert "error" in result["details"]


@respx.mock
async def test_connection_network_error():
    respx.get(f"{BASE_URL}/models").mock(side_effect=httpx.ConnectError("refused"))
    result = await make_connector().test_connection()
    assert result["connected"] is False


# ---------------------------------------------------------------------------
# stream_chat_completion
# ---------------------------------------------------------------------------

def _sse_body(*contents: str, finish: bool = True) -> bytes:
    lines = []
    for c in contents:
        chunk = {"choices": [{"delta": {"content": c}}]}
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    if finish:
        finish_chunk = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        lines.append(f"data: {json.dumps(finish_chunk)}")
        lines.append("")
        lines.append("data: [DONE]")
        lines.append("")
    return "\n".join(lines).encode()


@respx.mock
async def test_stream_yields_tokens():
    respx.post(f"{BASE_URL}/chat/completions").respond(
        200,
        content=_sse_body("Hello", " World"),
        headers={"content-type": "text/event-stream"},
    )
    connector = make_connector()
    tokens = []
    async for token in connector.stream_chat_completion([{"role": "user", "content": "Hi"}]):
        tokens.append(token)
    assert tokens == ["Hello", " World"]


@respx.mock
async def test_stream_uses_config_defaults():
    route = respx.post(f"{BASE_URL}/chat/completions").respond(
        200,
        content=_sse_body("ok"),
        headers={"content-type": "text/event-stream"},
    )
    connector = make_connector(model="phi3", max_tokens=512, temperature=0.5)
    async for _ in connector.stream_chat_completion([{"role": "user", "content": "hi"}]):
        pass
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "phi3"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.5
    assert body["stream"] is True


@respx.mock
async def test_stream_overrides_model_and_params():
    route = respx.post(f"{BASE_URL}/chat/completions").respond(
        200,
        content=_sse_body("ok"),
        headers={"content-type": "text/event-stream"},
    )
    connector = make_connector()
    async for _ in connector.stream_chat_completion(
        [{"role": "user", "content": "hi"}],
        model="mistral",
        temperature=0.1,
        max_tokens=256,
    ):
        pass
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "mistral"
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 256


@respx.mock
async def test_stream_skips_empty_delta():
    body = (
        b'data: {"choices": [{"delta": {"content": "A"}}]}\n\n'
        b'data: {"choices": [{"delta": {}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(f"{BASE_URL}/chat/completions").respond(
        200, content=body, headers={"content-type": "text/event-stream"}
    )
    tokens = []
    async for token in make_connector().stream_chat_completion([]):
        tokens.append(token)
    assert tokens == ["A"]


@respx.mock
async def test_stream_raises_on_http_error():
    respx.post(f"{BASE_URL}/chat/completions").respond(502)
    with pytest.raises(httpx.HTTPStatusError):
        async for _ in make_connector().stream_chat_completion([]):
            pass


@respx.mock
async def test_stream_sends_authorization_header():
    route = respx.post(f"{BASE_URL}/chat/completions").respond(
        200, content=_sse_body("tok"), headers={"content-type": "text/event-stream"}
    )
    async for _ in make_connector(api_key="sk-secret").stream_chat_completion([]):
        pass
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-secret"


def test_headers_omits_authorization_when_api_key_empty():
    headers = make_connector(api_key="")._headers()
    assert "Authorization" not in headers


def test_headers_includes_authorization_when_api_key_set():
    headers = make_connector(api_key="sk-test")._headers()
    assert headers["Authorization"] == "Bearer sk-test"


# ---------------------------------------------------------------------------
# reasoning_content tracking
# ---------------------------------------------------------------------------

@respx.mock
async def test_stream_reasoning_only_yields_no_tokens(caplog):
    """When the model only returns reasoning_content (no content), yield nothing and log warning."""
    import logging

    body = (
        b'data: {"choices": [{"delta": {"reasoning_content": "I am thinking..."}}]}\n\n'
        b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )
    respx.post(f"{BASE_URL}/chat/completions").respond(
        200, content=body, headers={"content-type": "text/event-stream"}
    )
    tokens = []
    with caplog.at_level(logging.WARNING, logger="aubergeRP.connectors.openai_text"):
        async for token in make_connector().stream_chat_completion([{"role": "user", "content": "Hi"}]):
            tokens.append(token)

    assert tokens == []
    assert any("reasoning" in record.message.lower() for record in caplog.records)


@respx.mock
async def test_stream_reasoning_field_variant(caplog):
    """Also handles 'reasoning' field (used by some APIs) the same way."""
    import logging

    body = (
        b'data: {"choices": [{"delta": {"reasoning": "Thinking step..."}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(f"{BASE_URL}/chat/completions").respond(
        200, content=body, headers={"content-type": "text/event-stream"}
    )
    tokens = []
    with caplog.at_level(logging.WARNING, logger="aubergeRP.connectors.openai_text"):
        async for token in make_connector().stream_chat_completion([]):
            tokens.append(token)

    assert tokens == []
    assert any("reasoning" in record.message.lower() for record in caplog.records)


@respx.mock
async def test_stream_reasoning_with_content_no_warning(caplog):
    """When both reasoning and content are present, no warning is emitted."""
    import logging

    body = (
        b'data: {"choices": [{"delta": {"reasoning_content": "thinking", "content": "Hello"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    respx.post(f"{BASE_URL}/chat/completions").respond(
        200, content=body, headers={"content-type": "text/event-stream"}
    )
    tokens = []
    with caplog.at_level(logging.WARNING, logger="aubergeRP.connectors.openai_text"):
        async for token in make_connector().stream_chat_completion([]):
            tokens.append(token)

    assert tokens == ["Hello"]
    assert not any(
        "reasoning" in record.message.lower() and record.levelno == logging.WARNING
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# custom_json
# ---------------------------------------------------------------------------


def test_custom_json_merged_with_lowest_priority():
    conn = make_connector(
        custom_json={"model": "ignored", "temperature": 0.1, "provider": {"order": ["a"]}},
        extra_body={"provider": {"order": ["b"]}},
    )
    payload = conn._build_payload(
        [], None, None, None, None, None, None, None, None, None
    )
    # Computed values win over custom_json…
    assert payload["model"] == "llama3"
    assert payload["temperature"] == 0.8
    # …and extra_body wins over custom_json too.
    assert payload["provider"] == {"order": ["b"]}


def test_custom_json_adds_unknown_fields():
    conn = make_connector(custom_json={"seed": 42})
    payload = conn._build_payload(
        [], None, None, None, None, None, None, None, None, None
    )
    assert payload["seed"] == 42


def test_debug_request_payload_matches_tool_call_body():
    conn = make_connector()
    messages = [{"role": "system", "content": "full prompt"}]
    tools = [{"type": "function", "function": {"name": "generate_image"}}]

    payload = conn.build_request_payload(messages, tools=tools)

    assert payload["messages"] == messages
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"
    assert payload["model"] == "llama3"
    assert payload["temperature"] == 0.8


def test_custom_json_empty_string_coerced():
    conn = make_connector(custom_json="")
    assert conn.config.custom_json == {}
