from pathlib import Path

import pytest

from aubergeRP.utils.png_metadata import read_png_metadata, write_png_metadata

FIXTURES = Path(__file__).parent / "fixtures"


def test_read_v1_png():
    card = read_png_metadata(FIXTURES / "sample_character_v1.png")
    assert card is not None
    assert card["name"] == "Elara"
    assert "description" in card


def test_read_sillytavern_real_v1_png():
    """Real SillyTavern V1 card downloaded from aicharactercards.com."""
    card = read_png_metadata(FIXTURES / "Princess-Elowen-Ashveil-aicharactercards.com_.png")
    assert card is not None
    assert card["name"] == "Princess Elowen Ashveil"
    assert "description" in card
    assert "personality" in card
    assert "first_mes" in card
    # Fields at root level (not nested under data), spec indicates V3 chara_card format
    assert card.get("spec") == "chara_card_v3"


def test_read_v2_png():
    card = read_png_metadata(FIXTURES / "sample_character_v2.png")
    assert card is not None
    assert card["spec"] == "chara_card_v2"
    assert card["data"]["name"] == "Darian"
    assert card["data"]["tags"] == ["minstrel", "human", "tavern"]


def test_read_returns_none_for_png_without_chara(tmp_path):
    import struct
    import zlib

    def chunk(t, d):
        crc = struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return struct.pack(">I", len(d)) + t + d + crc

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\xff\xff"
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    plain_png = tmp_path / "plain.png"
    plain_png.write_bytes(sig + ihdr + idat + iend)

    assert read_png_metadata(plain_png) is None


def test_read_raises_on_non_png(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png at all")
    with pytest.raises(ValueError, match="Not a valid PNG"):
        read_png_metadata(bad)


def test_roundtrip_v2_card(tmp_path):
    card = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "RoundTrip",
            "description": "Testing round-trip encode/decode.",
            "tags": ["test"],
            "extensions": {"aubergerp": {"image_prompt_prefix": "", "negative_prompt": ""}},
        },
    }
    src = FIXTURES / "sample_character_v1.png"
    dst = tmp_path / "out.png"
    write_png_metadata(src, dst, card)
    result = read_png_metadata(dst)
    assert result == card


def test_roundtrip_replaces_existing_chara_chunk(tmp_path):
    src = FIXTURES / "sample_character_v1.png"
    mid = tmp_path / "mid.png"
    dst = tmp_path / "dst.png"

    card_a = {"name": "First"}
    card_b = {"name": "Second"}

    write_png_metadata(src, mid, card_a)
    assert read_png_metadata(mid)["name"] == "First"

    write_png_metadata(mid, dst, card_b)
    assert read_png_metadata(dst)["name"] == "Second"


def test_roundtrip_preserves_image_data(tmp_path):
    src = FIXTURES / "sample_character_v2.png"
    dst = tmp_path / "out.png"
    card = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "X", "description": "Y"}}
    write_png_metadata(src, dst, card)

    import struct

    data = dst.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    found_ihdr = False
    found_idat = False
    found_iend = False
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        t = data[pos + 4 : pos + 8]
        if t == b"IHDR":
            found_ihdr = True
        if t == b"IDAT":
            found_idat = True
        if t == b"IEND":
            found_iend = True
        pos += 12 + length
    assert found_ihdr and found_idat and found_iend
