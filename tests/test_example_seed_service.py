from __future__ import annotations

import json
from pathlib import Path

from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.example_seed_service import seed_example_characters


def _write_example(path: Path, name: str, description: str) -> None:
    payload = {
        "spec": "chara_card_v2",
        "spec_version": "3.0",
        "data": {
            "name": name,
            "description": description,
            "tags": ["example"],
            "extensions": {},
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_seed_examples_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)

    _write_example(examples_dir / "captain_nemo.json", "Captain Nemo", "Commander of the Nautilus")

    # Avatar sidecar should be picked up when present.
    avatar = Path("tests/fixtures/sample_character_v2.png")
    (examples_dir / "captain_nemo.png").write_bytes(avatar.read_bytes())

    monkeypatch.setattr(
        "aubergeRP.services.example_seed_service._EXAMPLE_CHARACTERS_DIR",
        examples_dir,
    )

    seed_example_characters(tmp_path)
    seed_example_characters(tmp_path)

    svc = CharacterService(data_dir=tmp_path)
    cards = svc.list_characters()
    assert len(cards) == 1

    card = svc.get_character(cards[0].id)
    ext = card.data.extensions.get("aubergerp", {})
    assert isinstance(ext, dict)
    assert ext.get("seed_example_slug") == "captain_nemo"
    assert card.has_avatar is True

    state_path = tmp_path / "example_seed_state.json"
    assert state_path.exists()


def test_seed_examples_logs_and_continues_on_error(monkeypatch, tmp_path: Path, caplog) -> None:
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir(parents=True)

    (examples_dir / "broken.json").write_text("{bad json", encoding="utf-8")
    _write_example(examples_dir / "valid.json", "Valid Example", "Still imported")

    monkeypatch.setattr(
        "aubergeRP.services.example_seed_service._EXAMPLE_CHARACTERS_DIR",
        examples_dir,
    )

    seed_example_characters(tmp_path)

    svc = CharacterService(data_dir=tmp_path)
    cards = svc.list_characters()
    assert len(cards) == 1
    assert cards[0].name == "Valid Example"

    assert "Failed to import example character 'broken'" in caplog.text


def test_bundled_example_cards_round_trip_with_proactive_extensions(tmp_path: Path) -> None:
    svc = CharacterService(data_dir=tmp_path)
    examples_dir = Path("aubergeRP/examples/characters")
    for path in examples_dir.glob("*.json"):
        card = svc.import_character_json(path.read_bytes(), filename=path.name)
        exported = svc.export_character_json(card.id)
        reimported = svc.import_character_json(json.dumps(exported).encode("utf-8"))
        ext = reimported.data.extensions.get("aubergerp", {})
        assert isinstance(ext, dict)
        proactive = ext.get("proactive", {})
        schedules = ext.get("schedules", [])
        assert isinstance(proactive, dict)
        assert isinstance(schedules, list)
        for sched in schedules:
            assert sched.get("type") in {"daily_at", "daily_window", "after_delay", "after_inactivity"}
