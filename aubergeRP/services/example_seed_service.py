from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ..utils.file_storage import read_json, write_json
from .character_service import CharacterService

logger = logging.getLogger(__name__)

_EXAMPLE_CHARACTERS_DIR = Path(__file__).parent.parent / "examples" / "characters"
_SEED_STATE_FILE = "example_seed_state.json"


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = read_json(path)
    except Exception:
        logger.exception("Failed to read seed state at %s; starting with empty state", path)
        return {}
    entries = raw.get("characters") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_state(path: Path, characters: dict[str, Any]) -> None:
    write_json(path, {"characters": characters})


def _find_seeded_character_id(service: CharacterService, slug: str) -> str | None:
    for summary in service.list_characters():
        card = service.get_character(summary.id)
        ext = card.data.extensions.get("aubergerp", {})
        if isinstance(ext, dict) and ext.get("seed_example_slug") == slug:
            return card.id
    return None


def _inject_seed_metadata(raw: dict[str, Any], slug: str, checksum: str) -> None:
    data = raw.get("data")
    if not isinstance(data, dict):
        raise ValueError("Invalid character card: missing object 'data'")
    ext = data.get("extensions")
    if ext is None:
        ext = {}
        data["extensions"] = ext
    if not isinstance(ext, dict):
        raise ValueError("Invalid character card: 'extensions' must be an object")
    auberge_ext = ext.get("aubergerp")
    if auberge_ext is None:
        auberge_ext = {}
        ext["aubergerp"] = auberge_ext
    if not isinstance(auberge_ext, dict):
        raise ValueError("Invalid character card: 'extensions.aubergerp' must be an object")
    auberge_ext["seed_example_slug"] = slug
    auberge_ext["seed_example_checksum"] = checksum


def seed_example_characters(data_dir: str | Path) -> None:
    """Import bundled example characters into DB without overwriting user data.

    This is idempotent: each example file is imported at most once per checksum.
    Any per-file failure is logged and skipped so startup can continue.
    """
    examples_dir = _EXAMPLE_CHARACTERS_DIR
    if not examples_dir.exists():
        return

    data_path = Path(data_dir)
    state_path = data_path / _SEED_STATE_FILE
    state = _load_state(state_path)
    changed = False

    service = CharacterService(data_dir=data_path)

    for json_path in sorted(examples_dir.glob("*.json")):
        slug = json_path.stem
        try:
            content = json_path.read_bytes()
            checksum = _sha256_bytes(content)

            entry = state.get(slug, {})
            if isinstance(entry, dict) and entry.get("checksum") == checksum:
                continue

            existing_id = _find_seeded_character_id(service, slug)
            if existing_id is not None:
                state[slug] = {
                    "checksum": checksum,
                    "character_id": existing_id,
                    "source": json_path.name,
                }
                changed = True
                continue

            raw = json.loads(content)
            if not isinstance(raw, dict):
                raise ValueError("Invalid character card: root must be an object")

            _inject_seed_metadata(raw, slug, checksum)
            payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
            card = service.import_character_json(payload, filename=json_path.name)

            avatar_path = examples_dir / f"{slug}.png"
            if avatar_path.exists():
                try:
                    service.save_avatar(card.id, avatar_path.read_bytes())
                except Exception:
                    logger.exception(
                        "Failed to attach avatar %s for example '%s'", avatar_path, slug
                    )

            state[slug] = {
                "checksum": checksum,
                "character_id": card.id,
                "source": json_path.name,
            }
            changed = True
        except Exception:
            logger.exception("Failed to import example character '%s' from %s", slug, json_path)
            continue

    if changed:
        _save_state(state_path, state)
