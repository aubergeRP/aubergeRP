"""Prompt management service.

All LLM prompts live exclusively as .txt files under aubergeRP/prompts/.
The service reads each file at call time so that edits (via the admin UI or
directly on disk) take effect without restarting the server.

PROMPT_DEFAULTS is populated at startup by reading those same .txt files.
It serves two purposes:
  1. In-memory fallback if a file is deleted after the server starts.
  2. Reset target — restores the content that was on disk at server start-up
     (i.e. the factory default on a fresh install).

Prompt text must NOT be duplicated inside this Python module; the .txt files
are the single source of truth.
"""
from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Human-readable labels and descriptions shown in the admin UI.
PROMPT_META: dict[str, dict[str, str]] = {
    "default_system": {
        "label": "Default System Prompt",
        "description": (
            "Fallback system prompt used when a character has no custom system prompt. "
            "Supports {{char}} and {{user}} macros."
        ),
    },
    "image_marker_instruction": {
        "label": "Image Marker Instruction",
        "description": (
            "Appended to the system prompt when the LLM backend does not support tool "
            "calling. Instructs the model to emit [IMG: ...] markers."
        ),
    },
    "image_tool_instruction": {
        "label": "Image Tool Instruction",
        "description": (
            "Appended to the system prompt when the LLM backend supports tool calling. "
            "Instructs the model to call the generate_image tool."
        ),
    },
    "roleplay_bracket_instruction": {
        "label": "Roleplay Bracket Instruction",
        "description": (
            "Appended to the system prompt to explain how to handle bracketed/braced "
            "roleplay directions vs. spoken dialogue."
        ),
    },
    "ooc_guardrail": {
        "label": "OOC Protection Guardrail",
        "description": (
            "Injected when an out-of-character jailbreak attempt is detected. "
            "Reminds the model to stay in character."
        ),
    },
    "nsfw_block_guardrail": {
        "label": "NSFW Block Guardrail",
        "description": (
            "Injected when NSFW content is detected but the active connector has NSFW disabled."
        ),
    },
    "nsfw_allow_guardrail": {
        "label": "NSFW Allow Guardrail",
        "description": (
            "Injected when NSFW content is detected and the active connector has NSFW enabled."
        ),
    },
    "summarization_system": {
        "label": "Summarization System Prompt",
        "description": (
            "System prompt sent to the LLM when summarizing older conversation turns "
            "to stay within the context window."
        ),
    },
    "summarization_user": {
        "label": "Summarization User Prompt",
        "description": (
            "User message sent alongside the summarization system prompt. "
            "Must contain the {excerpt} placeholder."
        ),
    },
    "no_reasoning_instruction": {
        "label": "No-Reasoning Instruction",
        "description": (
            "Appended to the system prompt to instruct reasoning/thinking models not to "
            "place character dialogue or actions inside their internal reasoning section. "
            "Helps prevent empty responses when using models like DeepSeek-R1 or Qwen3."
        ),
    },
    "dialogue_only_instruction": {
        "label": "Dialogue-Only Channel Instruction",
        "description": (
            "Injected as a late system message when narration_mode='dialogue_only' is active. "
            "Instructs the model to reply only with text the character would actually send, "
            "suppressing narration, actions, and scene descriptions."
        ),
    },
    "image_prompt": {
        "label": "Image Prompt Template",
        "description": (
            "Template used to build a detailed image-generation prompt from the current "
            "roleplay scene. Supports {char_name}, {char_description}, {char_scenario}, "
            "{recent_exchanges}, and {raw_keywords} placeholders."
        ),
    },
    "proactive_event": {
        "label": "Proactive Event Injection",
        "description": (
            "Injected as a late system message when a scheduled proactive message is triggered. "
            "Tells the character why it is initiating the conversation. "
            "Supports {{local_time}} and {{instruction}} placeholders."
        ),
    },
    "character_translation": {
        "label": "Character Card Translation",
        "description": (
            "Used by the one-click character translation button in the admin panel. "
            "Supports {target_language} and {fields_json} placeholders; must ask the model "
            "to return a JSON object with the same keys."
        ),
    },
    "proactive_decision": {
        "label": "Proactive Send/Skip Decision",
        "description": (
            "Instruction used by the proactive engine to decide whether to SEND or SKIP. "
            "Must return strict JSON with action/message/reason fields."
        ),
    },
}


def _load_prompt_defaults() -> dict[str, str]:
    """Read factory-default content from the shipped .txt files at import time.

    Keys whose .txt file is absent or empty at startup are omitted.
    ``image_prompt`` is intentionally excluded — it has no reset target.
    """
    defaults: dict[str, str] = {}
    for key in PROMPT_META:
        if key == "image_prompt":
            continue
        path = _PROMPTS_DIR / f"{key}.txt"
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                defaults[key] = text
        except OSError:
            pass
    return defaults


# Populated from the shipped .txt files at server start-up.
# Used as in-memory fallback and as the "reset" target.
PROMPT_DEFAULTS: dict[str, str] = _load_prompt_defaults()


def get_prompt(key: str) -> str:
    """Return the current text for *key*, reading from disk first."""
    path = _PROMPTS_DIR / f"{key}.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    return PROMPT_DEFAULTS.get(key, "")


def save_prompt(key: str, content: str) -> None:
    """Persist *content* for *key* to disk."""
    if key not in set(PROMPT_DEFAULTS) | {"image_prompt"}:
        raise ValueError(f"Unknown prompt key: {key!r}")
    _PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    (_PROMPTS_DIR / f"{key}.txt").write_text(content, encoding="utf-8")


def reset_prompt(key: str) -> str:
    """Restore the shipped default for *key* and return it."""
    if key == "image_prompt":
        # image_prompt has no embedded default — leave the file as-is
        return get_prompt(key)
    if key not in PROMPT_DEFAULTS:
        raise ValueError(f"Unknown prompt key: {key!r}")
    default = PROMPT_DEFAULTS[key]
    save_prompt(key, default)
    return default


def list_prompts() -> list[dict[str, object]]:
    """Return metadata for every manageable prompt."""
    result = []
    for key, meta in PROMPT_META.items():
        path = _PROMPTS_DIR / f"{key}.txt"
        is_default = key in PROMPT_DEFAULTS
        current = get_prompt(key)
        default_text = PROMPT_DEFAULTS.get(key, "")
        result.append({
            "key": key,
            "label": meta["label"],
            "description": meta["description"],
            "content": current,
            "is_customized": bool(path.exists() and is_default and current != default_text),
            "has_reset": is_default,
        })
    return result
