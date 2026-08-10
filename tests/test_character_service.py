"""Tests for CharacterService — direct service calls, no HTTP."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import (
    CharacterImportError,
    CharacterNotFoundError,
    CharacterService,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Minimal 1×1 white PNG bytes (used as dummy avatar carrier)
import struct
import zlib as _zlib


def _minimal_png() -> bytes:
    def chunk(t, d):
        crc = struct.pack(">I", _zlib.crc32(t + d) & 0xFFFFFFFF)
        return struct.pack(">I", len(d)) + t + d + crc
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", _zlib.compress(b"\x00\xff\xff\xff"))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend

MINIMAL_PNG = _minimal_png()


def make_service(tmp_path: Path, default_avatar: Path | None = None) -> CharacterService:
    return CharacterService(data_dir=tmp_path, default_avatar=default_avatar)


def valid_data(**overrides) -> CharacterData:
    base = dict(name="Elara", description="An elven ranger.")
    base.update(overrides)
    return CharacterData(**base)


# ---------------------------------------------------------------------------
# create_character
# ---------------------------------------------------------------------------

def test_create_returns_card_with_id(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    assert card.id
    assert card.data.name == "Elara"
    assert card.spec == "chara_card_v2"


def test_create_persists_to_db(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    # verify it can be retrieved from the DB
    found = svc.get_character(card.id)
    assert found.id == card.id


def test_create_ensures_auberge_extension(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    assert "aubergerp" in card.data.extensions
    assert "image_prompt_prefix" in card.data.extensions["aubergerp"]


def test_create_preserves_existing_auberge_extension(tmp_path):
    svc = make_service(tmp_path)
    data = valid_data(extensions={"aubergerp": {"image_prompt_prefix": "elf", "negative_prompt": "blur"}})
    card = svc.create_character(data)
    assert card.data.extensions["aubergerp"]["image_prompt_prefix"] == "elf"


def test_create_merges_image_defaults_into_existing_auberge_extension(tmp_path):
    svc = make_service(tmp_path)
    schedules = [{"id": "morning", "type": "daily_window", "start": "08:00", "end": "09:00"}]
    data = valid_data(extensions={"aubergerp": {"schedules": schedules}})
    card = svc.create_character(data)
    ext = card.data.extensions["aubergerp"]
    assert ext["schedules"] == schedules
    assert ext["image_prompt_prefix"] == ""
    assert ext["negative_prompt"] == ""


def test_create_has_avatar_false_by_default(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    assert card.has_avatar is False


# ---------------------------------------------------------------------------
# get_character / list_characters
# ---------------------------------------------------------------------------

def test_get_character(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    found = svc.get_character(card.id)
    assert found.id == card.id
    assert found.data.name == "Elara"


def test_get_character_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterNotFoundError):
        svc.get_character("nonexistent")


def test_list_empty(tmp_path):
    assert make_service(tmp_path).list_characters() == []


def test_list_returns_summaries(tmp_path):
    svc = make_service(tmp_path)
    svc.create_character(valid_data(name="Elara"))
    svc.create_character(valid_data(name="Darian", description="A minstrel."))
    result = svc.list_characters()
    assert len(result) == 2
    names = {s.name for s in result}
    assert names == {"Elara", "Darian"}


def test_list_summary_has_avatar_url(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    summary = svc.list_characters()[0]
    assert summary.avatar_url == f"/api/characters/{card.id}/avatar"


# ---------------------------------------------------------------------------
# update_character
# ---------------------------------------------------------------------------

def test_update_character(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    updated = svc.update_character(card.id, valid_data(name="Updated", description="New desc."))
    assert updated.data.name == "Updated"
    assert updated.id == card.id


def test_update_preserves_created_at(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    updated = svc.update_character(card.id, valid_data(name="X"))
    assert updated.created_at == card.created_at


def test_update_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterNotFoundError):
        svc.update_character("bad-id", valid_data())


# ---------------------------------------------------------------------------
# delete_character
# ---------------------------------------------------------------------------

def test_delete_character(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.delete_character(card.id)
    with pytest.raises(CharacterNotFoundError):
        svc.get_character(card.id)


def test_delete_removes_avatar(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    svc.delete_character(card.id)
    assert not (tmp_path / "avatars" / f"{card.id}.png").exists()


def test_delete_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterNotFoundError):
        svc.delete_character("bad-id")


# ---------------------------------------------------------------------------
# duplicate_character
# ---------------------------------------------------------------------------

def test_duplicate_creates_new_id(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    dup = svc.duplicate_character(card.id)
    assert dup.id != card.id
    assert dup.data.name == card.data.name


def test_duplicate_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterNotFoundError):
        svc.duplicate_character("bad-id")


def test_duplicate_copies_avatar(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    dup = svc.duplicate_character(card.id)
    assert dup.has_avatar is True
    assert (tmp_path / "avatars" / f"{dup.id}.png").exists()


def test_duplicate_without_avatar(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    dup = svc.duplicate_character(card.id)
    assert dup.has_avatar is False


# ---------------------------------------------------------------------------
# import_character_json — V1
# ---------------------------------------------------------------------------

def test_import_json_v1(tmp_path):
    svc = make_service(tmp_path)
    v1 = FIXTURES / "sample_character_v1.json"
    card = svc.import_character_json(v1.read_bytes())
    assert card.spec == "chara_card_v2"
    assert card.data.name == "Elara"


def test_import_json_v2(tmp_path):
    svc = make_service(tmp_path)
    v2 = FIXTURES / "sample_character_v2.json"
    card = svc.import_character_json(v2.read_bytes())
    assert card.spec == "chara_card_v2"
    assert card.data.name == "Darian"
    assert card.data.tags == ["minstrel", "human", "tavern"]


def test_import_json_invalid_raises(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterImportError):
        svc.import_character_json(b"not json at all")


def test_import_json_missing_name_raises(tmp_path):
    svc = make_service(tmp_path)
    bad = json.dumps({"description": "no name here"}).encode()
    with pytest.raises(CharacterImportError, match="name"):
        svc.import_character_json(bad)


def test_import_json_missing_description_raises(tmp_path):
    svc = make_service(tmp_path)
    bad = json.dumps({"name": "X"}).encode()
    with pytest.raises(CharacterImportError, match="description"):
        svc.import_character_json(bad)


def test_import_json_ensures_auberge_extension(tmp_path):
    svc = make_service(tmp_path)
    raw = json.dumps({"name": "X", "description": "Y"}).encode()
    card = svc.import_character_json(raw)
    assert "aubergerp" in card.data.extensions


# ---------------------------------------------------------------------------
# import_character_png
# ---------------------------------------------------------------------------

def test_import_png_v1(tmp_path):
    svc = make_service(tmp_path)
    card = svc.import_character_png(
        (FIXTURES / "sample_character_v1.png").read_bytes()
    )
    assert card.spec == "chara_card_v2"
    assert card.data.name == "Elara"
    assert card.has_avatar is True


def test_import_png_v2(tmp_path):
    svc = make_service(tmp_path)
    card = svc.import_character_png(
        (FIXTURES / "sample_character_v2.png").read_bytes()
    )
    assert card.data.name == "Darian"


def test_import_png_saves_avatar(tmp_path):
    svc = make_service(tmp_path)
    content = (FIXTURES / "sample_character_v1.png").read_bytes()
    card = svc.import_character_png(content)
    avatar_path = tmp_path / "avatars" / f"{card.id}.png"
    assert avatar_path.exists()
    assert avatar_path.read_bytes() == content


def test_import_png_sillytavern_real(tmp_path):
    svc = make_service(tmp_path)
    content = (FIXTURES / "Princess-Elowen-Ashveil-aicharactercards.com_.png").read_bytes()
    card = svc.import_character_png(content)
    assert card.data.name == "Princess Elowen Ashveil"
    assert card.has_avatar is True


def test_import_png_without_chara_raises(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterImportError, match="chara"):
        svc.import_character_png(MINIMAL_PNG)


# ---------------------------------------------------------------------------
# export_character_json
# ---------------------------------------------------------------------------

def test_export_json_v2_format(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data(tags=["elf"]))
    exported = svc.export_character_json(card.id)
    assert exported["spec"] == "chara_card_v2"
    assert exported["spec_version"] == "2.0"
    assert exported["data"]["name"] == "Elara"
    assert exported["data"]["tags"] == ["elf"]


def test_export_json_strips_wrapper_fields(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    exported = svc.export_character_json(card.id)
    assert "id" not in exported
    assert "has_avatar" not in exported
    assert "created_at" not in exported
    assert "updated_at" not in exported


def test_export_json_not_found(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(CharacterNotFoundError):
        svc.export_character_json("bad-id")


# ---------------------------------------------------------------------------
# export_character_png
# ---------------------------------------------------------------------------

def test_export_png_uses_avatar_as_carrier(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    png_bytes = svc.export_character_png(card.id)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    # Verify the card is embedded
    from aubergeRP.utils.png_metadata import read_png_metadata
    recovered = read_png_metadata(png_bytes)
    assert recovered is not None
    assert recovered["data"]["name"] == "Elara"


def test_export_png_uses_default_avatar_fallback(tmp_path):
    default_png = tmp_path / "default.png"
    default_png.write_bytes(MINIMAL_PNG)
    svc = make_service(tmp_path, default_avatar=default_png)
    card = svc.create_character(valid_data())
    png_bytes = svc.export_character_png(card.id)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_export_png_no_avatar_no_default_raises(tmp_path):
    svc = make_service(tmp_path, default_avatar=tmp_path / "nonexistent.png")
    card = svc.create_character(valid_data())
    with pytest.raises(Exception):
        svc.export_character_png(card.id)


# ---------------------------------------------------------------------------
# save_avatar / get_avatar_path
# ---------------------------------------------------------------------------

def test_save_avatar_writes_file(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    assert (tmp_path / "avatars" / f"{card.id}.png").read_bytes() == MINIMAL_PNG


def test_save_avatar_sets_has_avatar(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    reloaded = svc.get_character(card.id)
    assert reloaded.has_avatar is True


def test_get_avatar_path_none_when_missing(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    assert svc.get_avatar_path(card.id) is None


def test_get_avatar_path_returns_path(tmp_path):
    svc = make_service(tmp_path)
    card = svc.create_character(valid_data())
    svc.save_avatar(card.id, MINIMAL_PNG)
    p = svc.get_avatar_path(card.id)
    assert p is not None and p.exists()
