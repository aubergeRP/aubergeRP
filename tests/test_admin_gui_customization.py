"""Tests for admin GUI customization."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aubergeRP.config import GuiConfig, _strip_js, reset_config

# ---------------------------------------------------------------------------
# Direct unit tests for _strip_js and GuiConfig validator
# ---------------------------------------------------------------------------

def test_strip_js_removes_script_tags():
    result = _strip_js('<p>Hello</p><script>alert(1)</script>')
    assert "<script" not in result
    assert "<p>Hello</p>" in result


def test_strip_js_removes_script_with_whitespace_in_closing_tag():
    result = _strip_js('<script>evil()</script \t\n bar>')
    assert "evil" not in result


def test_strip_js_removes_event_handlers():
    result = _strip_js('<div onclick="evil()">text</div>')
    assert "onclick" not in result
    assert "text" in result


def test_strip_js_removes_javascript_href():
    result = _strip_js('<a href="javascript:alert(1)">link</a>')
    assert "javascript:" not in result


def test_guiconfig_validator_strips_script():
    cfg = GuiConfig(custom_header_html='<p>ok</p><script>evil()</script>')
    assert "<script" not in cfg.custom_header_html
    assert "<p>ok</p>" in cfg.custom_header_html


def test_guiconfig_validator_strips_footer_event_handler():
    cfg = GuiConfig(custom_footer_html='<img src="x" onerror="bad()">')
    assert "onerror" not in cfg.custom_footer_html


# ---------------------------------------------------------------------------
# App fixture — fresh config per test, save path redirected to tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUBERGE_DATA_DIR", str(tmp_path))
    reset_config()
    from aubergeRP.main import create_app
    from aubergeRP.routers.admin import get_admin_token
    from aubergeRP.routers.config import get_config_save_path
    app = create_app()
    config_file = tmp_path / "config.yaml"
    app.dependency_overrides[get_config_save_path] = lambda: config_file
    app.dependency_overrides[get_admin_token] = lambda: "test-admin-token"
    with TestClient(app) as c:
        yield c
    reset_config()


def test_gui_config_defaults(client):
    """GET /api/config/gui returns default empty strings."""
    resp = client.get("/api/config/gui")
    assert resp.status_code == 200
    data = resp.json()
    assert data["custom_css"] == ""
    assert data["custom_header_html"] == ""
    assert data["custom_footer_html"] == ""
    assert data["public_character_list"] is True


def test_gui_config_public_character_list_round_trip(client):
    """The unlisted-chats switch persists through PUT and is returned by GET."""
    payload = {
        "custom_css": "",
        "custom_header_html": "",
        "custom_footer_html": "",
        "public_character_list": False,
    }
    assert client.put("/api/config/gui", json=payload).status_code == 200
    assert client.get("/api/config/gui").json()["public_character_list"] is False


def test_gui_config_update_and_retrieve(client):
    """PUT /api/config/gui persists values and GET returns them."""
    payload = {
        "custom_css": "body { background: red; }",
        "custom_header_html": "<marquee>Hello</marquee>",
        "custom_footer_html": "<p>Custom footer</p>",
    }
    put_resp = client.put("/api/config/gui", json=payload)
    assert put_resp.status_code == 200
    data = put_resp.json()
    assert data["custom_css"] == payload["custom_css"]
    assert data["custom_header_html"] == payload["custom_header_html"]
    assert data["custom_footer_html"] == payload["custom_footer_html"]

    # Re-fetch to verify in-memory persistence
    get_resp = client.get("/api/config/gui")
    assert get_resp.status_code == 200
    assert get_resp.json() == data


def test_gui_config_partial_update(client):
    """Updating only one field works; others default to empty string."""
    payload = {"custom_css": "a { color: blue; }", "custom_header_html": "", "custom_footer_html": ""}
    resp = client.put("/api/config/gui", json=payload)
    assert resp.status_code == 200
    assert resp.json()["custom_css"] == "a { color: blue; }"
    assert resp.json()["custom_header_html"] == ""


def test_gui_config_script_tags_stripped(client):
    """<script> tags in HTML fields are removed before storage."""
    payload = {
        "custom_css": "",
        "custom_header_html": '<p>Hello</p><script>alert(1)</script>',
        "custom_footer_html": '<script src="evil.js"></script><p>footer</p>',
    }
    resp = client.put("/api/config/gui", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "<script" not in data["custom_header_html"]
    assert "<p>Hello</p>" in data["custom_header_html"]
    assert "<script" not in data["custom_footer_html"]
    assert "<p>footer</p>" in data["custom_footer_html"]


def test_gui_config_inline_event_handlers_stripped(client):
    """Inline event handlers (onclick, onload, …) are removed."""
    payload = {
        "custom_css": "",
        "custom_header_html": '<div onclick="evil()">click me</div>',
        "custom_footer_html": '<img src="x" onerror="alert(1)">',
    }
    resp = client.put("/api/config/gui", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "onclick" not in data["custom_header_html"]
    assert "onerror" not in data["custom_footer_html"]
