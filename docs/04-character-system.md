# Character System

## Format

aubergeRP uses **SillyTavern V2** (`chara_card_v2`) as its native format, extended with a small wrapper:

```json
{
  "id": "uuid-v4",
  "has_avatar": true,
  "created_at": "2025-01-15T10:30:00Z",
  "updated_at": "2025-01-15T10:30:00Z",
  "spec": "chara_card_v2",
  "spec_version": "2.0",
  "data": {
    "name": "Elara",
    "description": "Full character description. Supports {{user}} and {{char}}.",
    "personality": "Warm, welcoming, wise.",
    "first_mes": "Welcome, traveler!",
    "mes_example": "<START>\n{{user}}: Hello\n{{char}}: Greetings!",
    "scenario": "A medieval fantasy tavern.",
    "system_prompt": "",
    "post_history_instructions": "",
    "creator": "",
    "creator_notes": "",
    "tags": ["fantasy", "elf"],
    "extensions": {
      "aubergerp": {
        "image_prompt_prefix": "elf woman, fantasy tavern",
        "negative_prompt": "blurry, low quality"
      }
    }
  }
}
```

## Import / Export

| Format | Import | Export |
|---|---|---|
| JSON V2 (`spec: "chara_card_v2"`) | ✅ | ✅ |
| JSON V1 (flat root fields) | ✅ (auto-upgraded) | — |
| PNG (tEXt chunk `chara`) V1 or V2 | ✅ | ✅ |

Export strips the wrapper fields (`id`, `has_avatar`, timestamps) — the result is a standard SillyTavern card.

## Translation

The admin panel offers a one-click **Translate…** action on each character
(`POST /api/characters/{id}/translate` with `{"language": "French"}`).

It is non-destructive: the card is duplicated (avatar included) and only the
copy's free-text fields (`description`, `personality`, `first_mes`,
`mes_example`, `scenario`, `system_prompt`, `post_history_instructions`,
`creator_notes`) are replaced by the translation. The copy's name gets a
` (<language>)` suffix; `name`, `tags` and `creator` are never translated.

The active **text** connector performs the translation using the
`character_translation` prompt (admin-editable). The model must answer with a
JSON object using the same keys; if it doesn't, the request fails with HTTP 502
and no copy is created. See `services/character_translation_service.py`.

## Macros

| Macro | Replaced with |
|---|---|
| `{{char}}` | Character's `data.name` |
| `{{user}}` | `user.name` from `config.yaml` |

Macros are resolved at prompt-build time (in `chat_service.py`), not at storage time.

## aubergeRP extensions (`data.extensions.aubergerp`)

All aubergeRP-specific settings live under a single, **lowercase** key,
`data.extensions.aubergerp`:

| Field | Description |
|---|---|
| `image_prompt_prefix` | Prepended to every image generation prompt for this character |
| `negative_prompt` | Default negative prompt for image generation |
| `proactive` | Proactive-messaging defaults (`enabled`, `decision_mode`, cooldowns, limits) |
| `schedules` | List of schedule definitions — when the character messages first |

`proactive` and `schedules` are documented in full in
[07-character-card-schedules.md](07-character-card-schedules.md). They survive
import/export, so a card can ship its own behaviour; per-user runtime state is
kept separately in the database and is never written back to the card.
