"""One-click translation of a character card via the active text connector.

The translation is non-destructive: the original card is left untouched and a
new card (avatar included) is created with the translated text fields.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..models.character import CharacterCard
from ..services.character_service import CharacterService
from ..services.observability_service import record_error
from ..services.prompt_service import get_prompt

logger = logging.getLogger(__name__)

# Free-text fields worth translating. ``name``, ``tags`` and ``creator`` are
# intentionally left alone (proper nouns / metadata).
TRANSLATABLE_FIELDS = (
    "description",
    "personality",
    "first_mes",
    "mes_example",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "creator_notes",
)


class CharacterTranslationError(RuntimeError):
    pass


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object out of an LLM reply, tolerating surrounding prose."""
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _build_prompt(fields: dict[str, str], target_language: str) -> list[dict[str, str]]:
    # ``str.replace`` (not ``format``) — the prompt contains {{char}} macros.
    template = get_prompt("character_translation")
    content = (
        template
        .replace("{target_language}", target_language)
        .replace("{fields_json}", json.dumps(fields, ensure_ascii=False, indent=2))
    )
    return [{"role": "user", "content": content}]


async def translate_character(
    character_id: str,
    target_language: str,
    *,
    service: CharacterService,
    text_connector: Any,
) -> CharacterCard:
    """Create a translated copy of *character_id* in *target_language*."""
    target_language = target_language.strip()
    if not target_language:
        raise CharacterTranslationError("Target language is required")

    original = service.get_character(character_id)
    fields = {
        key: value
        for key in TRANSLATABLE_FIELDS
        if isinstance(value := getattr(original.data, key, ""), str) and value.strip()
    }
    if not fields:
        raise CharacterTranslationError("This character has no text to translate")

    messages = _build_prompt(fields, target_language)
    try:
        chunks = [chunk async for chunk in text_connector.stream_chat_completion(messages)]
    except Exception as exc:
        logger.exception("Character translation failed for %s", character_id)
        record_error("character_translation", str(exc))
        raise CharacterTranslationError(f"Translation request failed: {exc}") from exc

    translated = _extract_json_object("".join(chunks).strip())
    if translated is None:
        record_error("character_translation", "LLM returned no parsable JSON object")
        raise CharacterTranslationError(
            "The model did not return a valid translation. Try again or use another model."
        )

    updates = {
        key: str(translated[key])
        for key in fields
        if isinstance(translated.get(key), str) and str(translated[key]).strip()
    }
    if not updates:
        raise CharacterTranslationError("The model returned an empty translation")

    # Duplicate first (copies the avatar), then overwrite the translated fields.
    copy = service.duplicate_character(character_id)
    updates["name"] = f"{original.data.name} ({target_language})"
    return service.update_character(copy.id, original.data.model_copy(update=updates))
