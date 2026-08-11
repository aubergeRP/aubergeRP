"""Integration tests for /api/characters via FastAPI TestClient."""
import io
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aubergeRP.main import create_app
from aubergeRP.routers.characters import get_character_service
from aubergeRP.services.character_service import CharacterService

FIXTURES = Path(__file__).parent / "fixtures"


def _minimal_png() -> bytes:
    def chunk(t, d):
        crc = struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return struct.pack(">I", len(d)) + t + d + crc
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend

MINIMAL_PNG = _minimal_png()


@pytest.fixture
def client(tmp_path):
    app = create_app()
    default_png = tmp_path / "default-avatar.png"
    default_png.write_bytes(MINIMAL_PNG)
    svc = CharacterService(data_dir=tmp_path, default_avatar=default_png)
    app.dependency_overrides[get_character_service] = lambda: svc
    with TestClient(app) as c:
        yield c


def create_char(client, name="Elara", description="An elven ranger.", **extra):
    payload = {"name": name, "description": description, **extra}
    resp = client.post("/api/characters/", json=payload)
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# GET /api/characters/
# ---------------------------------------------------------------------------

def test_list_empty(client):
    resp = client.get("/api/characters/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_returns_summaries(client):
    create_char(client, name="Elara")
    create_char(client, name="Darian", description="A minstrel.")
    resp = client.get("/api/characters/")
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    assert names == {"Elara", "Darian"}


def test_list_summary_shape(client):
    create_char(client)
    item = client.get("/api/characters/").json()[0]
    assert "id" in item
    assert "avatar_url" in item
    assert "has_avatar" in item
    assert "tags" in item
    assert "created_at" in item


# ---------------------------------------------------------------------------
# POST /api/characters/
# ---------------------------------------------------------------------------

def test_create_character(client):
    resp = client.post("/api/characters/", json={"name": "X", "description": "Y"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["spec"] == "chara_card_v2"
    assert data["data"]["name"] == "X"


def test_create_character_missing_name(client):
    resp = client.post("/api/characters/", json={"description": "No name"})
    assert resp.status_code == 422


def test_create_character_missing_description(client):
    resp = client.post("/api/characters/", json={"name": "No desc"})
    assert resp.status_code == 422


def test_create_character_with_tags(client):
    resp = client.post("/api/characters/", json={
        "name": "X", "description": "Y", "tags": ["elf", "ranger"]
    })
    assert resp.status_code == 201
    assert resp.json()["data"]["tags"] == ["elf", "ranger"]


# ---------------------------------------------------------------------------
# GET /api/characters/{id}
# ---------------------------------------------------------------------------

def test_get_character(client):
    card = create_char(client)
    resp = client.get(f"/api/characters/{card['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == card["id"]


def test_get_character_not_found(client):
    resp = client.get("/api/characters/nonexistent-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/characters/{id}
# ---------------------------------------------------------------------------

def test_update_character(client):
    card = create_char(client, name="Old")
    resp = client.put(f"/api/characters/{card['id']}", json={
        "name": "New", "description": "Updated description."
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "New"


def test_update_character_not_found(client):
    resp = client.put("/api/characters/bad-id", json={"name": "X", "description": "Y"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/characters/{id}
# ---------------------------------------------------------------------------

def test_delete_character(client):
    card = create_char(client)
    resp = client.delete(f"/api/characters/{card['id']}")
    assert resp.status_code == 204
    assert client.get(f"/api/characters/{card['id']}").status_code == 404


def test_delete_character_not_found(client):
    resp = client.delete("/api/characters/bad-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/characters/import
# ---------------------------------------------------------------------------

def test_import_json_v1(client):
    content = (FIXTURES / "sample_character_v1.json").read_bytes()
    resp = client.post(
        "/api/characters/import",
        files={"file": ("elara.json", io.BytesIO(content), "application/json")},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "Elara"


def test_import_json_v2(client):
    content = (FIXTURES / "sample_character_v2.json").read_bytes()
    resp = client.post(
        "/api/characters/import",
        files={"file": ("darian.json", io.BytesIO(content), "application/json")},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "Darian"


def test_import_png_v2(client):
    content = (FIXTURES / "sample_character_v2.png").read_bytes()
    resp = client.post(
        "/api/characters/import",
        files={"file": ("darian.png", io.BytesIO(content), "image/png")},
    )
    assert resp.status_code == 201
    assert resp.json()["has_avatar"] is True


def test_import_png_sillytavern(client):
    content = (FIXTURES / "Princess-Elowen-Ashveil-aicharactercards.com_.png").read_bytes()
    resp = client.post(
        "/api/characters/import",
        files={"file": ("princess.png", io.BytesIO(content), "image/png")},
    )
    assert resp.status_code == 201
    assert resp.json()["data"]["name"] == "Princess Elowen Ashveil"


def test_import_invalid_json(client):
    resp = client.post(
        "/api/characters/import",
        files={"file": ("bad.json", io.BytesIO(b"not json"), "application/json")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Avatar endpoints
# ---------------------------------------------------------------------------

def test_get_avatar_not_found(client):
    card = create_char(client)
    resp = client.get(f"/api/characters/{card['id']}/avatar")
    assert resp.status_code == 404


def test_upload_and_get_avatar(client):
    card = create_char(client)
    upload = client.post(
        f"/api/characters/{card['id']}/avatar",
        files={"file": ("avatar.png", io.BytesIO(MINIMAL_PNG), "image/png")},
    )
    assert upload.status_code == 200
    assert "avatar_url" in upload.json()

    resp = client.get(f"/api/characters/{card['id']}/avatar")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_upload_avatar_not_found(client):
    resp = client.post(
        "/api/characters/bad-id/avatar",
        files={"file": ("a.png", io.BytesIO(MINIMAL_PNG), "image/png")},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

def test_export_json(client):
    card = create_char(client, name="Tester")
    resp = client.get(f"/api/characters/{card['id']}/export/json")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()
    assert body["spec"] == "chara_card_v2"
    assert "id" not in body


def test_export_png_with_avatar(client):
    card = create_char(client)
    client.post(
        f"/api/characters/{card['id']}/avatar",
        files={"file": ("a.png", io.BytesIO(MINIMAL_PNG), "image/png")},
    )
    resp = client.get(f"/api/characters/{card['id']}/export/png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_png_uses_default_avatar(client):
    # No avatar uploaded — should fall back to default-avatar.png
    card = create_char(client)
    resp = client.get(f"/api/characters/{card['id']}/export/png")
    assert resp.status_code == 200
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_json_not_found(client):
    resp = client.get("/api/characters/bad-id/export/json")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

def test_duplicate_character(client):
    card = create_char(client, name="Original")
    resp = client.post(f"/api/characters/{card['id']}/duplicate")
    assert resp.status_code == 201
    dup = resp.json()
    assert dup["id"] != card["id"]
    assert dup["data"]["name"] == "Original"


def test_duplicate_not_found(client):
    resp = client.post("/api/characters/bad-id/duplicate")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Translate
# ---------------------------------------------------------------------------

class _FakeTextConnector:
    """Minimal text connector returning a canned reply, one chunk at a time."""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list = []

    async def stream_chat_completion(self, messages, **kwargs):
        self.prompts.append(messages)
        yield self.reply


def _patch_connector(monkeypatch, connector):
    import aubergeRP.routers.connectors as connectors_router

    class _Manager:
        def get_active_text_connector(self):
            return connector

    monkeypatch.setattr(connectors_router, "get_connector_manager", lambda: _Manager())


def test_translate_character_creates_copy(client, monkeypatch):
    card = create_char(client, name="Elara", description="An elven ranger.",
                       first_mes="Hello there!")
    conn = _FakeTextConnector(
        '{"description": "Une rôdeuse elfe.", "first_mes": "Bonjour !"}'
    )
    _patch_connector(monkeypatch, conn)

    resp = client.post(f"/api/characters/{card['id']}/translate", json={"language": "French"})
    assert resp.status_code == 201
    translated = resp.json()
    assert translated["id"] != card["id"]
    assert translated["data"]["name"] == "Elara (French)"
    assert translated["data"]["description"] == "Une rôdeuse elfe."
    assert translated["data"]["first_mes"] == "Bonjour !"

    # Original untouched
    original = client.get(f"/api/characters/{card['id']}").json()
    assert original["data"]["description"] == "An elven ranger."

    # The prompt carried the source fields and the target language
    prompt = conn.prompts[0][0]["content"]
    assert "French" in prompt
    assert "An elven ranger." in prompt


def test_translate_character_keeps_untranslated_fields(client, monkeypatch):
    card = create_char(client, name="Elara", description="An elven ranger.",
                       scenario="A forest at dusk.")
    _patch_connector(monkeypatch, _FakeTextConnector('{"description": "Une rôdeuse elfe."}'))
    resp = client.post(f"/api/characters/{card['id']}/translate", json={"language": "French"})
    assert resp.status_code == 201
    assert resp.json()["data"]["scenario"] == "A forest at dusk."


def test_translate_character_tolerates_surrounding_prose(client, monkeypatch):
    card = create_char(client)
    _patch_connector(
        monkeypatch,
        _FakeTextConnector('Sure!\n```json\n{"description": "Une rôdeuse."}\n```'),
    )
    resp = client.post(f"/api/characters/{card['id']}/translate", json={"language": "French"})
    assert resp.status_code == 201
    assert resp.json()["data"]["description"] == "Une rôdeuse."


def test_translate_character_invalid_llm_output(client, monkeypatch):
    card = create_char(client)
    _patch_connector(monkeypatch, _FakeTextConnector("I cannot do that."))
    resp = client.post(f"/api/characters/{card['id']}/translate", json={"language": "French"})
    assert resp.status_code == 502
    # No stray copy was created
    assert len(client.get("/api/characters/").json()) == 1


def test_translate_character_no_connector(client, monkeypatch):
    card = create_char(client)
    _patch_connector(monkeypatch, None)
    resp = client.post(f"/api/characters/{card['id']}/translate", json={"language": "French"})
    assert resp.status_code == 400


def test_translate_character_not_found(client, monkeypatch):
    _patch_connector(monkeypatch, _FakeTextConnector('{"description": "x"}'))
    resp = client.post("/api/characters/bad-id/translate", json={"language": "French"})
    assert resp.status_code == 404
