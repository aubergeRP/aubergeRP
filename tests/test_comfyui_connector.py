"""Unit tests for the ComfyUI connector."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from aubergeRP.connectors.comfyui import (
    _BUILTIN_WORKFLOWS_DIR,
    _NEGATIVE_PLACEHOLDER,
    _PROMPT_PLACEHOLDER,
    ComfyUIConnector,
)
from aubergeRP.models.connector import ComfyUIConfig

BASE_URL = "http://comfyui.local:8188"
FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"
PROMPT_ID = "test-prompt-id-123"


def make_connector(tmp_path: Path, **overrides: Any) -> ComfyUIConnector:
    defaults: dict[str, Any] = {"base_url": BASE_URL, "workflow": "default"}
    defaults.update(overrides)
    return ComfyUIConnector(ComfyUIConfig(**defaults), tmp_path / "comfyui_workflows")


def write_workflow(tmp_path: Path, name: str = "default", content: dict | None = None) -> None:
    wf_dir = tmp_path / "comfyui_workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    wf_content = content or {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": _PROMPT_PLACEHOLDER}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": _NEGATIVE_PLACEHOLDER}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }
    (wf_dir / f"{name}.json").write_text(json.dumps(wf_content), encoding="utf-8")


def make_history(filename: str = "image.png") -> dict:
    return {
        PROMPT_ID: {
            "outputs": {
                "9": {
                    "images": [{"filename": filename, "subfolder": "", "type": "output"}]
                }
            },
            "status": {"completed": True},
        }
    }


# ---------------------------------------------------------------------------
# _inject_prompt
# ---------------------------------------------------------------------------


def test_inject_prompt_replaces_placeholders(tmp_path: Path) -> None:
    conn = make_connector(tmp_path)
    workflow = {
        "6": {"inputs": {"text": _PROMPT_PLACEHOLDER}},
        "7": {"inputs": {"text": _NEGATIVE_PLACEHOLDER}},
    }
    result = conn._inject_prompt(workflow, "a red apple", "blurry")
    assert result["6"]["inputs"]["text"] == "a red apple"
    assert result["7"]["inputs"]["text"] == "blurry"


def test_inject_prompt_empty_negative(tmp_path: Path) -> None:
    conn = make_connector(tmp_path)
    workflow = {"6": {"inputs": {"text": _PROMPT_PLACEHOLDER}}}
    result = conn._inject_prompt(workflow, "forest scene", "")
    assert result["6"]["inputs"]["text"] == "forest scene"


def test_inject_prompt_escapes_quotes(tmp_path: Path) -> None:
    conn = make_connector(tmp_path)
    workflow = {"6": {"inputs": {"text": _PROMPT_PLACEHOLDER}}}
    result = conn._inject_prompt(workflow, 'she said "hello"', "")
    assert '"hello"' in result["6"]["inputs"]["text"]


# ---------------------------------------------------------------------------
# _load_workflow
# ---------------------------------------------------------------------------


def test_load_workflow_user_dir_takes_priority(tmp_path: Path) -> None:
    write_workflow(tmp_path, "default", {"user": "yes"})
    conn = make_connector(tmp_path)
    wf = conn._load_workflow()
    assert wf == {"user": "yes"}


def test_load_workflow_falls_back_to_builtin(tmp_path: Path) -> None:
    """If no user workflow exists, the built-in default should be loaded."""
    conn = make_connector(tmp_path)
    # Don't write any user workflow — rely on built-in
    if (_BUILTIN_WORKFLOWS_DIR / "default.json").exists():
        wf = conn._load_workflow()
        assert "__PROMPT__" in json.dumps(wf)


def test_load_workflow_missing_raises(tmp_path: Path) -> None:
    conn = make_connector(tmp_path, workflow="nonexistent_workflow")
    with pytest.raises(FileNotFoundError, match="nonexistent_workflow"):
        conn._load_workflow()


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def test_ws_url_http(tmp_path: Path) -> None:
    conn = make_connector(tmp_path, base_url="http://localhost:8188")
    assert conn._ws_url("abc").startswith("ws://localhost:8188/ws?clientId=abc")


def test_ws_url_https(tmp_path: Path) -> None:
    conn = make_connector(tmp_path, base_url="https://comfy.example.com")
    assert conn._ws_url("xyz").startswith("wss://comfy.example.com/ws?clientId=xyz")


def test_http_url(tmp_path: Path) -> None:
    conn = make_connector(tmp_path, base_url="http://localhost:8188/")
    assert conn._http_url("/prompt") == "http://localhost:8188/prompt"


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@respx.mock
async def test_connection_success(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/system_stats").respond(200, json={"system": "ok"})
    result = await make_connector(tmp_path).test_connection()
    assert result["connected"] is True
    assert result["details"] == {"system": "ok"}


@respx.mock
async def test_connection_failure(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/system_stats").respond(500)
    result = await make_connector(tmp_path).test_connection()
    assert result["connected"] is False


@respx.mock
async def test_connection_network_error(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/system_stats").mock(side_effect=httpx.ConnectError("refused"))
    result = await make_connector(tmp_path).test_connection()
    assert result["connected"] is False
    assert "error" in result["details"]


# ---------------------------------------------------------------------------
# generate_image (via mocked generate_image_with_progress)
# ---------------------------------------------------------------------------


async def test_generate_image_returns_bytes(tmp_path: Path) -> None:
    conn = make_connector(tmp_path)

    async def _fake_progress(*args: Any, **kwargs: Any):
        yield {"type": "complete", "bytes": FAKE_PNG}

    with patch.object(conn, "generate_image_with_progress", _fake_progress):
        result = await conn.generate_image("test prompt")
    assert result == FAKE_PNG


# ---------------------------------------------------------------------------
# generate_image_with_progress — WebSocket fallback to polling
# ---------------------------------------------------------------------------


@respx.mock
async def test_generate_image_with_progress_ws_fallback(tmp_path: Path) -> None:
    """When WebSocket is unavailable, should fall back to polling and return image."""
    write_workflow(tmp_path)
    conn = make_connector(tmp_path)

    # Mock HTTP endpoints
    respx.post(f"{BASE_URL}/prompt").respond(200, json={"prompt_id": PROMPT_ID})
    respx.get(f"{BASE_URL}/history/{PROMPT_ID}").respond(200, json=make_history())
    respx.get(f"{BASE_URL}/view").respond(200, content=FAKE_PNG)

    events = []
    # Test the real method with websockets patched to fail (WS not available)
    with patch("websockets.connect") as mock_ws_connect:
        mock_ws_connect.side_effect = OSError("connection refused")
        # Also patch poll to return immediately
        with patch.object(conn, "_poll_until_done", new_callable=AsyncMock):
            async for event in conn.generate_image_with_progress("a red apple"):
                events.append(event)

    assert any(e["type"] == "complete" for e in events)
    complete_events = [e for e in events if e["type"] == "complete"]
    assert complete_events[0]["bytes"] == FAKE_PNG


@respx.mock
async def test_submit_prompt_retries_transient_error(tmp_path: Path) -> None:
    """A 502 on /prompt is retried; a definitive 400 is not."""
    write_workflow(tmp_path)
    conn = make_connector(tmp_path, max_retries=2)

    route = respx.post(f"{BASE_URL}/prompt").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(200, json={"prompt_id": PROMPT_ID}),
        ]
    )
    assert await conn._submit_prompt({"1": {}}, "client-1") == PROMPT_ID
    assert route.call_count == 2


@respx.mock
async def test_submit_prompt_does_not_retry_definitive_error(tmp_path: Path) -> None:
    write_workflow(tmp_path)
    conn = make_connector(tmp_path, max_retries=3)

    route = respx.post(f"{BASE_URL}/prompt").respond(400)
    with pytest.raises(httpx.HTTPStatusError):
        await conn._submit_prompt({"1": {}}, "client-1")
    assert route.call_count == 1


@respx.mock
async def test_generate_image_with_progress_progress_events(tmp_path: Path) -> None:
    """WebSocket progress events are yielded as image_progress dicts."""
    write_workflow(tmp_path)
    conn = make_connector(tmp_path)

    respx.post(f"{BASE_URL}/prompt").respond(200, json={"prompt_id": PROMPT_ID})
    respx.get(f"{BASE_URL}/history/{PROMPT_ID}").respond(200, json=make_history())
    respx.get(f"{BASE_URL}/view").respond(200, content=FAKE_PNG)

    ws_messages = [
        json.dumps({"type": "progress", "data": {"value": 5, "max": 20}}),
        json.dumps({"type": "progress", "data": {"value": 10, "max": 20}}),
        json.dumps({"type": "executing", "data": {"prompt_id": PROMPT_ID, "node": None}}),
    ]

    class FakeWS:
        def __init__(self) -> None:
            self._msgs = iter(ws_messages)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._msgs)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    with patch("websockets.connect", return_value=FakeWS()):
        events = []
        async for event in conn.generate_image_with_progress("a dragon"):
            events.append(event)

    progress_events = [e for e in events if e["type"] == "progress"]
    assert len(progress_events) == 2
    assert progress_events[0]["step"] == 5
    assert progress_events[0]["total"] == 20
    assert progress_events[1]["step"] == 10

    complete_events = [e for e in events if e["type"] == "complete"]
    assert len(complete_events) == 1
    assert complete_events[0]["bytes"] == FAKE_PNG


# ---------------------------------------------------------------------------
# list_all_workflows
# ---------------------------------------------------------------------------


def test_list_all_workflows_includes_user_and_builtin(tmp_path: Path) -> None:
    write_workflow(tmp_path, "my_custom")
    conn = make_connector(tmp_path)
    workflows = conn.list_all_workflows()
    assert "my_custom" in workflows
    # Built-in default should also appear
    assert "default" in workflows


def test_list_all_workflows_no_duplicates(tmp_path: Path) -> None:
    write_workflow(tmp_path, "default")  # user copy of default
    conn = make_connector(tmp_path)
    workflows = conn.list_all_workflows()
    assert workflows.count("default") == 1


def test_list_user_workflows_empty_when_no_dir(tmp_path: Path) -> None:
    conn = make_connector(tmp_path)
    # No comfyui_workflows dir in tmp_path
    result = conn.list_user_workflows()
    assert result == []


# ---------------------------------------------------------------------------
# _fetch_result
# ---------------------------------------------------------------------------


@respx.mock
async def test_fetch_result_with_subfolder(tmp_path: Path) -> None:
    history = {
        PROMPT_ID: {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": "out.png", "subfolder": "sub", "type": "output"}
                    ]
                }
            }
        }
    }
    respx.get(f"{BASE_URL}/history/{PROMPT_ID}").respond(200, json=history)
    respx.get(f"{BASE_URL}/view").respond(200, content=FAKE_PNG)

    conn = make_connector(tmp_path)
    result = await conn._fetch_result(PROMPT_ID)
    assert result == FAKE_PNG


@respx.mock
async def test_fetch_result_no_outputs_raises(tmp_path: Path) -> None:
    respx.get(f"{BASE_URL}/history/{PROMPT_ID}").respond(200, json={PROMPT_ID: {"outputs": {}}})
    conn = make_connector(tmp_path)
    with pytest.raises(RuntimeError, match="No output image"):
        await conn._fetch_result(PROMPT_ID)
