"""Tests for chat_service — ImageMarkerParser, build_prompt, stream_chat."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from aubergeRP.models.character import CharacterCard, CharacterData
from aubergeRP.models.conversation import Conversation
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.chat_service import (
    IMAGE_FAILURE_MESSAGE,
    ChatGenerationError,
    ChatService,
    ImageMarkerParser,
    _format_user_message_for_llm,
    autonomy_allowed,
    build_prompt,
)
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.media_service import MediaService
from aubergeRP.services.observability_service import get_registry
from aubergeRP.services.prompt_service import get_prompt
from aubergeRP.services.statistics_service import StatisticsService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(UTC)


def _char(**overrides) -> CharacterCard:
    base = dict(name="Elara", description="An elven ranger.")
    base.update(overrides)
    data = CharacterData(**base)
    return CharacterCard(id="c1", has_avatar=False, created_at=_now(), updated_at=_now(), data=data)


def _conv(char: CharacterCard, messages=None) -> Conversation:
    return Conversation(
        id="conv-1",
        character_id=char.id,
        character_name=char.data.name,
        title=char.data.name,
        messages=messages or [],
        created_at=_now(),
        updated_at=_now(),
    )


def make_services(tmp_path: Path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    return char_svc, conv_svc


class _FakeText:
    """Async generator connector that yields preset tokens."""
    connector_type = "text"

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        for t in self._tokens:
            yield t

    async def test_connection(self) -> dict:
        return {"connected": True}


class _RecordingText:
    connector_type = "text"
    supports_tool_calling = False

    def __init__(self, *call_responses: list[str]) -> None:
        self._responses = list(call_responses) or [[]]
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        self.calls.append(messages)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        for token in self._responses[idx]:
            yield token

    async def test_connection(self) -> dict:
        return {"connected": True}


class _FailText:
    """Text connector that raises on stream."""
    connector_type = "text"

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        raise RuntimeError("connection refused")
        yield  # make it a generator

    async def test_connection(self) -> dict:
        return {}


class _FakeImage:
    """Image connector that returns fake bytes."""
    connector_type = "image"

    def __init__(self, img: bytes = b"PNG") -> None:
        self._img = img

    async def generate_image(self, prompt, **kw) -> bytes:
        return self._img

    async def generate_image_with_progress(self, prompt, **kw):
        yield {"type": "complete", "bytes": self._img}

    async def test_connection(self) -> dict:
        return {}


class _FailImage:
    connector_type = "image"

    async def generate_image(self, prompt, **kw) -> bytes:
        raise RuntimeError("image gen failed")

    async def generate_image_with_progress(self, prompt, **kw):
        if False:  # make this an async generator while always raising
            yield {}
        raise RuntimeError("image gen failed")

    async def test_connection(self) -> dict:
        return {}


def _manager(text_conn=None, image_conn=None, role_conns=None) -> MagicMock:
    m = MagicMock()
    m.get_active_text_connector.return_value = text_conn
    m.get_active_image_connector.return_value = image_conn
    roles = role_conns or {}
    m.get_text_connector.side_effect = lambda role="text": roles.get(role, text_conn)
    return m


def make_chat_service(
    tmp_path: Path,
    text_conn=None,
    image_conn=None,
    context_window: int = 4096,
    summarization_threshold: float = 0.75,
    role_conns=None,
) -> tuple:
    char_svc, conv_svc = make_services(tmp_path)
    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(text_conn, image_conn, role_conns),
        images_dir=tmp_path / "images",
        context_window=context_window,
        summarization_threshold=summarization_threshold,
    )
    return char_svc, conv_svc, svc


async def collect(gen) -> list[dict[str, Any]]:
    return [e async for e in gen]


# ---------------------------------------------------------------------------
# ImageMarkerParser — plain text
# ---------------------------------------------------------------------------

def test_parser_plain_text():
    p = ImageMarkerParser()
    events = p.feed("Hello world")
    assert events == [{"type": "token", "text": "Hello world"}]


def test_parser_empty_string():
    p = ImageMarkerParser()
    assert p.feed("") == []


def test_parser_flush_clean():
    p = ImageMarkerParser()
    p.feed("some text")
    assert p.flush() == []


# ---------------------------------------------------------------------------
# ImageMarkerParser — single marker
# ---------------------------------------------------------------------------

def test_parser_single_image():
    p = ImageMarkerParser()
    events = p.feed("[IMG:a dragon]")
    assert events == [{"type": "image_trigger", "prompt": "a dragon"}]


def test_parser_empty_prompt():
    p = ImageMarkerParser()
    events = p.feed("[IMG:]")
    assert events == [{"type": "image_trigger", "prompt": ""}]


def test_parser_text_before_image():
    p = ImageMarkerParser()
    events = p.feed("Look: [IMG:cat]")
    assert events[0] == {"type": "token", "text": "Look: "}
    assert events[1] == {"type": "image_trigger", "prompt": "cat"}


def test_parser_text_after_image():
    p = ImageMarkerParser()
    events = p.feed("[IMG:cat] Done")
    assert events[0] == {"type": "image_trigger", "prompt": "cat"}
    assert events[1] == {"type": "token", "text": " Done"}


def test_parser_multiple_images():
    p = ImageMarkerParser()
    events = p.feed("[IMG:cat][IMG:dog]")
    assert len(events) == 2
    assert events[0]["prompt"] == "cat"
    assert events[1]["prompt"] == "dog"


# ---------------------------------------------------------------------------
# ImageMarkerParser — false starts / edge cases
# ---------------------------------------------------------------------------

def test_parser_false_bracket():
    p = ImageMarkerParser()
    events = p.feed("[not an image]")
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "[not an image]"


def test_parser_false_img_prefix():
    p = ImageMarkerParser()
    events = p.feed("[IMGUR:url]")
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "[IMGU" in text or text == "[IMGUR:url]"
    assert all(e["type"] == "token" for e in events)


def test_parser_split_marker_across_chunks():
    p = ImageMarkerParser()
    e1 = p.feed("[IMG:")
    assert e1 == []  # prefix matched but marker not closed
    e2 = p.feed("a dragon]")
    assert e2 == [{"type": "image_trigger", "prompt": "a dragon"}]


def test_parser_split_prefix_across_chunks():
    p = ImageMarkerParser()
    e1 = p.feed("[IM")
    assert e1 == []
    e2 = p.feed("G:prompt]")
    assert e2 == [{"type": "image_trigger", "prompt": "prompt"}]


def test_parser_flush_unclosed_marker():
    p = ImageMarkerParser()
    p.feed("[IMG:unclosed")
    events = p.flush()
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "[IMG:unclosed" in text


def test_parser_flush_partial_prefix():
    p = ImageMarkerParser()
    p.feed("[IM")
    events = p.flush()
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "[IM" in text


def test_parser_text_then_image_then_text():
    p = ImageMarkerParser()
    events = p.feed("Before [IMG:x] After")
    types = [e["type"] for e in events]
    assert types == ["token", "image_trigger", "token"]
    assert events[1]["prompt"] == "x"


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_has_system_with_description():
    char = _char()
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    assert "An elven ranger." in system["content"]


def test_build_prompt_includes_system_prompt():
    char = _char(system_prompt="You are {{char}}.")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = msgs[0]
    assert "You are Elara." in system["content"]


def test_build_prompt_includes_personality():
    char = _char(personality="Brave and witty.")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    assert "Brave and witty." in system["content"]


def test_build_prompt_includes_scenario():
    char = _char(scenario="In a dark tavern.")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    assert "In a dark tavern." in system["content"]


def test_build_prompt_no_system_when_empty():
    char = _char(description="Desc")
    # description is always set; check we still get system
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    assert any(m["role"] == "system" for m in msgs)


def _assistant(images: list[str] | None = None):
    import uuid

    from aubergeRP.models.conversation import Message
    return Message(
        id=str(uuid.uuid4()), role="assistant", content="…",
        images=images or [], timestamp=_now(),
    )


# --- image instruction / autonomy ---

@pytest.mark.parametrize("use_tools", [False, True])
def test_build_prompt_no_image_instruction_without_image_connector(use_tools):
    char = _char()
    conv = _conv(char)
    msgs = build_prompt(conv, char, use_tool_calling=use_tools, image_enabled=False)
    system = msgs[0]["content"]
    assert "[IMG:" not in system
    assert "generate_image" not in system


def test_build_prompt_strict_image_instruction_by_default():
    char = _char()
    conv = _conv(char)
    system = build_prompt(conv, char)[0]["content"]
    assert get_prompt("image_marker_instruction") in system
    assert get_prompt("image_marker_instruction_autonomous") not in system


def test_build_prompt_autonomous_marker_instruction():
    char = _char()
    conv = _conv(char)
    system = build_prompt(conv, char, image_autonomy=True)[0]["content"]
    assert get_prompt("image_marker_instruction_autonomous") in system


def test_build_prompt_autonomous_tool_instruction():
    char = _char()
    conv = _conv(char)
    system = build_prompt(
        conv, char, use_tool_calling=True, image_autonomy=True
    )[0]["content"]
    assert get_prompt("image_tool_instruction_autonomous") in system


def test_autonomy_allowed_on_empty_conversation():
    char = _char()
    assert autonomy_allowed(_conv(char), 4) is True


def test_autonomy_blocked_when_last_reply_had_an_image():
    char = _char()
    conv = _conv(char, messages=[_assistant(["/img/a.png"])])
    assert autonomy_allowed(conv, 4) is False


def test_autonomy_allowed_once_cooldown_elapsed():
    char = _char()
    conv = _conv(char, messages=[_assistant(["/img/a.png"]), _assistant(), _assistant()])
    assert autonomy_allowed(conv, 2) is True
    assert autonomy_allowed(conv, 4) is False


def test_autonomy_allowed_when_cooldown_disabled():
    char = _char()
    conv = _conv(char, messages=[_assistant(["/img/a.png"])])
    assert autonomy_allowed(conv, 0) is True


def test_build_prompt_includes_history():
    from aubergeRP.models.conversation import Message
    char = _char()
    msg = Message(id="m1", role="user", content="Hello!", images=[], timestamp=_now())
    conv = _conv(char, messages=[msg])
    msgs = build_prompt(conv, char)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert any(m["content"] == "Hello!" for m in user_msgs)


def test_build_prompt_mes_example_parsed():
    char = _char(mes_example="<START>\n{{user}}: Hi!\n{{char}}: Hello, traveller!")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    # mes_example is now included in the system message, not as separate roles
    system = next(m for m in msgs if m["role"] == "system")
    assert "Example dialogue" in system["content"]
    assert "Hi!" in system["content"]
    assert "Hello, traveller!" in system["content"]


def test_build_prompt_post_history_instructions():
    char = _char(post_history_instructions="Always reply as {{char}}.")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    last = msgs[-1]
    assert last["role"] == "system"
    assert "Always reply as Elara." in last["content"]


def test_build_prompt_resolves_user_macro():
    char = _char(system_prompt="Speak to {{user}}.")
    conv = _conv(char)
    msgs = build_prompt(conv, char, user_name="Bob")
    system = msgs[0]
    assert "Bob" in system["content"]


def test_build_prompt_includes_no_reasoning_instruction():
    """The system prompt must include the no-reasoning instruction for reasoning model safety."""
    from aubergeRP.services.prompt_service import get_prompt
    char = _char()
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    expected = get_prompt("no_reasoning_instruction")
    assert expected in system["content"]


def test_format_user_message_for_llm_without_brackets_is_unchanged():
    content = "Hello there"
    assert _format_user_message_for_llm(content) == content


def test_format_user_message_for_llm_extracts_dialogue_and_instructions():
    content = "I step closer. [whispering] Hello {smiles}"
    formatted = _format_user_message_for_llm(content)
    assert "Dialogue:" in formatted
    assert "I step closer. Hello" in formatted
    assert "Roleplay instructions (non-dialogue):" in formatted
    assert "- whispering" in formatted
    assert "- smiles" in formatted


def test_format_user_message_for_llm_with_only_instructions():
    content = "[looks around] {listens carefully}"
    formatted = _format_user_message_for_llm(content)
    assert "Dialogue:" not in formatted
    assert "Roleplay instructions (non-dialogue):" in formatted
    assert "- looks around" in formatted
    assert "- listens carefully" in formatted


def test_format_user_message_for_llm_unclosed_bracket_is_unchanged():
    content = "Hello [whispering"
    assert _format_user_message_for_llm(content) == content


def test_build_prompt_formats_user_roleplay_bracket_content():
    from aubergeRP.models.conversation import Message

    char = _char()
    user_msg = Message(
        id="m1",
        role="user",
        content="I move forward. [quietly]",
        images=[],
        timestamp=_now(),
    )
    conv = _conv(char, messages=[user_msg])

    msgs = build_prompt(conv, char)
    llm_user = next(m for m in msgs if m["role"] == "user")
    assert "Dialogue:" in llm_user["content"]
    assert "I move forward." in llm_user["content"]
    assert "Roleplay instructions (non-dialogue):" in llm_user["content"]
    assert "- quietly" in llm_user["content"]


# ---------------------------------------------------------------------------
# Markdown formatting rules in system prompt
# ---------------------------------------------------------------------------

def test_default_system_prompt_contains_markdown_rules():
    """The default system prompt must include Markdown formatting instructions."""
    from aubergeRP.services.prompt_service import get_prompt
    prompt = get_prompt("default_system")
    assert "Markdown" in prompt
    assert "italic" in prompt.lower()
    assert "bold" in prompt.lower()


def test_default_system_prompt_contains_stage_direction_example():
    """The default system prompt must explain how to format stage directions."""
    from aubergeRP.services.prompt_service import get_prompt
    prompt = get_prompt("default_system")
    # Stage directions use *[...]*
    assert "*[" in prompt


def test_roleplay_bracket_instruction_references_markdown_italic():
    """The bracket instruction must tell the LLM to use Markdown italic for stage directions."""
    from aubergeRP.services.prompt_service import get_prompt
    instruction = get_prompt("roleplay_bracket_instruction")
    assert "*[" in instruction


def test_build_prompt_default_system_includes_markdown_formatting():
    """When no custom system prompt is set, built prompt must contain Markdown rules."""
    char = _char()
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    assert "Markdown" in system["content"]
    assert "*[" in system["content"]


def test_build_prompt_custom_system_prompt_no_markdown_override():
    """A custom system prompt is used as-is; Markdown rules are not injected into it."""
    char = _char(system_prompt="Custom prompt without markdown.")
    conv = _conv(char)
    msgs = build_prompt(conv, char)
    system = next(m for m in msgs if m["role"] == "system")
    # The custom prompt is the base; the roleplay_bracket_instruction (which now
    # references Markdown italic) is still appended, but the base itself is custom.
    assert "Custom prompt without markdown." in system["content"]


# ---------------------------------------------------------------------------
# stream_chat — no connector
# ---------------------------------------------------------------------------

async def test_stream_no_text_connector(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=None)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert events[0]["type"] == "error"


async def test_stream_invalid_conversation(tmp_path):
    _, _, svc = make_chat_service(tmp_path, text_conn=_FakeText(["Hi"]))
    events = await collect(svc.stream_chat("nonexistent", "Hi"))
    assert events[0]["type"] == "error"


# ---------------------------------------------------------------------------
# stream_chat — token streaming
# ---------------------------------------------------------------------------

async def test_stream_yields_tokens(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText(["Hello", " world"]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 2
    assert tokens[0]["content"] == "Hello"
    assert tokens[1]["content"] == " world"


async def test_stream_ends_with_done(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText(["Hi"]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hello"))
    assert events[-1]["type"] == "done"


async def test_stream_records_llm_call_statistics(tmp_path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    stats_svc = StatisticsService(data_dir=tmp_path)

    manager = _manager(_FakeText(["Hello", " world"]), None)
    manager.get_active_id_for_type.return_value = ""

    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=manager,
        images_dir=tmp_path / "images",
        statistics_service=stats_svc,
    )

    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert events[-1]["type"] == "done"

    dashboard = stats_svc.get_dashboard_data(days=7, top=5)
    assert dashboard["summary"]["llm_calls"] == 1
    assert events[-1]["full_content"] == "Hello world"


async def test_stream_saves_assistant_message(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText(["Hello"]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    await collect(svc.stream_chat(conv.id, "Hi"))
    reloaded = conv_svc.get_conversation(conv.id)
    roles = [m.role for m in reloaded.messages]
    assert "user" in roles
    assert "assistant" in roles


async def test_stream_connector_error(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FailText())
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert any(e["type"] == "error" for e in events)
    reloaded = conv_svc.get_conversation(conv.id)
    assert reloaded.messages == []


# ---------------------------------------------------------------------------
# generate_reply (internal non-HTTP API)
# ---------------------------------------------------------------------------

async def test_generate_reply_non_streaming(tmp_path):
    text = _RecordingText(["Hello", " world"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    result = await svc.generate_reply(conv.id, "Hi")

    assert result.text == "Hello world"
    assert result.message_id
    assert result.images == []
    assert len(text.calls) == 1
    assert any(m["role"] == "user" and m["content"] == "Hi" for m in text.calls[0])


async def test_generate_reply_persists_user_and_assistant_messages(tmp_path):
    text = _RecordingText(["Assistant reply"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    await svc.generate_reply(conv.id, "Hello there")

    reloaded = conv_svc.get_conversation(conv.id)
    assert [m.role for m in reloaded.messages] == ["user", "assistant"]
    assert [m.content for m in reloaded.messages] == ["Hello there", "Assistant reply"]


async def test_generate_reply_uses_existing_history(tmp_path):
    text = _RecordingText(["I remember Pixel."])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)
    conv_svc.append_message(conv.id, "user", "mon chien s'appelle Pixel")
    conv_svc.append_message(conv.id, "assistant", "joli nom")

    await svc.generate_reply(conv.id, "comment s'appelle mon chien ?")

    sent_messages = text.calls[0]
    assert any(
        m["role"] == "user" and "mon chien s'appelle Pixel" in m["content"]
        for m in sent_messages
    )
    assert any(
        m["role"] == "assistant" and "joli nom" in m["content"]
        for m in sent_messages
    )


async def test_generate_reply_uses_character_configuration(tmp_path):
    text = _RecordingText(["En garde!"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(
        name="Elara",
        description="An elven ranger.",
        personality="Brave and witty.",
        scenario="Inside an old tavern.",
        first_mes="",
    ))
    conv = conv_svc.create_conversation(char.id)

    await svc.generate_reply(conv.id, "hello")

    system_prompt = text.calls[0][0]["content"]
    assert "Elara's description: An elven ranger." in system_prompt
    assert "Elara's personality: Brave and witty." in system_prompt
    assert "Scenario: Inside an old tavern." in system_prompt


async def test_generate_reply_uses_summarization_pipeline(tmp_path):
    text = _RecordingText(["summary text"], ["final reply"])
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=text,
        context_window=300,
        summarization_threshold=0.5,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)
    for idx in range(8):
        role = "user" if idx % 2 == 0 else "assistant"
        conv_svc.append_message(conv.id, role, f"{role}-{idx} " + ("a" * 320))

    await svc.generate_reply(conv.id, "latest")

    assert len(text.calls) >= 2
    summary_call = text.calls[0]
    generation_call = text.calls[-1]
    assert len(summary_call) == 2
    assert summary_call[0]["role"] == "system"
    assert summary_call[1]["role"] == "user"
    assert any(
        m["role"] == "system"
        and str(m["content"]).startswith("[Summary of earlier conversation]")
        for m in generation_call
    )


async def test_summarization_uses_its_own_connector(tmp_path):
    """Multi-model: the summary runs on the `text_summarization` connector."""
    chat = _RecordingText(["final reply"])
    summarizer = _RecordingText(["summary text"])
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=chat,
        context_window=300,
        summarization_threshold=0.5,
        role_conns={"text_summarization": summarizer},
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)
    for idx in range(8):
        role = "user" if idx % 2 == 0 else "assistant"
        conv_svc.append_message(conv.id, role, f"{role}-{idx} " + ("a" * 320))

    await svc.generate_reply(conv.id, "latest")

    assert len(summarizer.calls) == 1
    assert summarizer.calls[0][0]["role"] == "system"
    assert len(chat.calls) == 1
    assert any(
        m["role"] == "system"
        and str(m["content"]).startswith("[Summary of earlier conversation]")
        for m in chat.calls[0]
    )


async def test_generate_reply_handles_connector_failure(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FailText())
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    reloaded = conv_svc.get_conversation(conv.id)
    assert reloaded.messages == []


async def test_generate_reply_rolls_back_user_message_when_no_text_connector(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=None)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    reloaded = conv_svc.get_conversation(conv.id)
    assert reloaded.messages == []


# ---------------------------------------------------------------------------
# stream_chat — image trigger
# ---------------------------------------------------------------------------

async def test_stream_image_trigger_emits_start(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:a cat]"]),
        image_conn=_FakeImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert any(e["type"] == "image_start" for e in events)
    assert any(e["type"] == "image_complete" for e in events)


async def test_stream_image_trigger_url_in_done(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:a cat]"]),
        image_conn=_FakeImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    done = next(e for e in events if e["type"] == "done")
    assert len(done["images"]) == 1
    assert done["images"][0].startswith("/api/images/")


async def test_stream_image_no_connector(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:cat]"]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert any(e["type"] == "image_failed" for e in events)
    assert events[-1]["type"] == "done"


async def test_stream_image_connector_failure(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:cat]"]),
        image_conn=_FailImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    get_registry().reset()
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    failed = next(e for e in events if e["type"] == "image_failed")
    # The cause goes to the admin error tail, not to the end user.
    assert failed["detail"] == IMAGE_FAILURE_MESSAGE
    assert "image gen failed" not in failed["detail"]
    errors = get_registry().list_errors(component="image")
    assert len(errors) == 1
    assert "image gen failed" in errors[0].summary
    assert errors[0].conversation_id == conv.id


async def test_stream_image_failure_detail_no_mdn_url(tmp_path):
    """The recorded error keeps the HTTP status without httpx's MDN URL."""

    class _HttpErrorImage:
        connector_type = "image"

        async def generate_image_with_progress(self, prompt, **kw):
            if False:
                yield {}
            import httpx
            httpx.Response(400, json={"error": {"message": "Provider returned error"}})
            raise ValueError("[OpenRouter Chat API] HTTP 400: Provider returned error")

        async def test_connection(self) -> dict:
            return {}

    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:tavern scene]"]),
        image_conn=_HttpErrorImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    get_registry().reset()
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    failed = next(e for e in events if e["type"] == "image_failed")
    assert failed["detail"] == IMAGE_FAILURE_MESSAGE
    recorded = get_registry().list_errors(component="image")[0].summary
    assert "developer.mozilla.org" not in recorded
    assert "HTTP 400" in recorded


async def test_stream_image_failure_logs_prompt(tmp_path, caplog):
    """The image prompt is included in the ERROR log when generation fails."""
    import logging

    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:dragon breathing fire]"]),
        image_conn=_FailImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    with caplog.at_level(logging.ERROR, logger="aubergeRP.services.chat_service"):
        await collect(svc.stream_chat(conv.id, "Hi"))
    assert any("dragon breathing fire" in r.message for r in caplog.records)


async def test_retry_generate_image_success(tmp_path):
    """retry_generate_image yields image_complete on success."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        image_conn=_FakeImage(b"RETRIED"),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.retry_generate_image(conv.id, "a sunset", "gen-retry-1"))
    assert any(e["type"] == "image_complete" for e in events)
    complete = next(e for e in events if e["type"] == "image_complete")
    assert complete["generation_id"] == "gen-retry-1"


async def test_retry_generate_image_failure(tmp_path):
    """retry_generate_image yields image_failed when connector raises."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        image_conn=_FailImage(),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    get_registry().reset()
    events = await collect(svc.retry_generate_image(conv.id, "a sunset", "gen-retry-2"))
    failed = next(e for e in events if e["type"] == "image_failed")
    assert failed["generation_id"] == "gen-retry-2"
    assert failed["detail"] == IMAGE_FAILURE_MESSAGE
    assert "image gen failed" in get_registry().list_errors(component="image")[0].summary


async def test_stream_image_saves_to_disk(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["[IMG:x]"]),
        image_conn=_FakeImage(b"PNGDATA"),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    complete = next(e for e in events if e["type"] == "image_complete")
    filename = complete["image_url"].split("/")[-1]
    assert (tmp_path / "images" / filename).read_bytes() == b"PNGDATA"


async def test_stream_image_persists_prompt_in_media_library(tmp_path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    media_svc = MediaService(data_dir=tmp_path)

    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(_FakeText(["[IMG:a cinematic tavern scene]"]), _FakeImage()),
        images_dir=tmp_path / "images",
        media_service=media_svc,
    )

    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Hi"))
    done = next(e for e in events if e["type"] == "done")
    assert len(done["images"]) == 1

    medias, _total = media_svc.list_media()
    assert len(medias) == 1
    assert medias[0].raw_prompt == "a cinematic tavern scene"
    assert medias[0].media_url == done["images"][0]


# ---------------------------------------------------------------------------
# _generate_image_prompt
# ---------------------------------------------------------------------------

class _MultiCallFakeText:
    """Text connector that yields different token lists on successive calls."""

    connector_type = "text"

    def __init__(self, *call_responses: list[str]) -> None:
        self._responses = list(call_responses)
        self._call_count = 0

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        for t in self._responses[idx]:
            yield t


class _RecordingImage:
    """Image connector that records the last prompt it received."""

    connector_type = "image"
    last_prompt: str = ""

    async def generate_image(self, prompt, **kw) -> bytes:
        self.last_prompt = prompt
        return b"PNG"

    async def generate_image_with_progress(self, prompt, **kw):
        self.last_prompt = prompt
        yield {"type": "complete", "bytes": b"PNG"}

    async def test_connection(self) -> dict:
        return {}


async def test_generate_image_prompt_returns_llm_output(tmp_path):
    text_conn = _FakeText(["enhanced image prompt"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    result = await svc._generate_image_prompt(
        text_conn,
        char,
        [{"role": "user", "content": "show me"}],
        "elf ranger forest",
    )
    assert result.prompt == "enhanced image prompt"


async def test_generate_image_prompt_fallback_on_error(tmp_path):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, _FailText())
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    result = await svc._generate_image_prompt(
        _FailText(),
        char,
        [],
        "original keywords",
    )
    assert result.prompt == "original keywords"


async def test_stream_image_uses_llm_enhanced_prompt(tmp_path):
    img_conn = _RecordingImage()
    text_conn = _MultiCallFakeText(
        ["[IMG:elf ranger]"],          # first call: main narrative
        ["detailed enhanced prompt"],  # second call: image prompt generation
    )
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn, img_conn)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Show me the scene"))
    assert any(e["type"] == "image_complete" for e in events)
    assert img_conn.last_prompt == "detailed enhanced prompt"


async def test_stream_image_media_records_every_prompt_step(tmp_path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    media_svc = MediaService(data_dir=tmp_path)

    text_conn = _MultiCallFakeText(
        ["[IMG:original keywords]"],
        ["llm enhanced prompt"],
    )
    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(text_conn, _FakeImage()),
        images_dir=tmp_path / "images",
        media_service=media_svc,
    )

    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)

    await collect(svc.stream_chat(conv.id, "Hi"))
    medias, _total = media_svc.list_media()
    assert len(medias) == 1
    # `prompt` is what the connector really received; the marker keywords and
    # the LLM answer are kept alongside it so the admin can audit the chain.
    assert medias[0].prompt == "llm enhanced prompt"
    assert medias[0].raw_prompt == "original keywords"
    assert medias[0].llm_output_prompt == "llm enhanced prompt"
    assert "original keywords" in medias[0].llm_input_prompt


# ---------------------------------------------------------------------------
# _generate_image_prompt — template content
# ---------------------------------------------------------------------------

class _CapturingText:
    """Text connector that records every message list it receives."""

    connector_type = "text"

    def __init__(self, response: str = "enhanced prompt") -> None:
        self._response = response
        self.received: list[list[dict]] = []

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        self.received.append(list(messages))
        yield self._response

    async def test_connection(self) -> dict:
        return {}


async def test_generate_image_prompt_includes_char_name(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="Seraphina", description="A warrior."))
    await svc._generate_image_prompt(conn, char, [], "battle")
    user_content = conn.received[0][0]["content"]
    assert "Seraphina" in user_content


async def test_generate_image_prompt_includes_char_description(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="A legendary blacksmith with a scarred face."))
    await svc._generate_image_prompt(conn, char, [], "forge")
    user_content = conn.received[0][0]["content"]
    assert "legendary blacksmith" in user_content


async def test_generate_image_prompt_includes_scenario(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y", scenario="A dark forest at midnight."))
    await svc._generate_image_prompt(conn, char, [], "trees")
    user_content = conn.received[0][0]["content"]
    assert "dark forest" in user_content


async def test_generate_image_prompt_includes_raw_keywords(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    await svc._generate_image_prompt(conn, char, [], "dragon attacking castle")
    user_content = conn.received[0][0]["content"]
    assert "dragon attacking castle" in user_content


async def test_generate_image_prompt_requires_english_output(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    await svc._generate_image_prompt(conn, char, [], "scene")
    user_content = conn.received[0][0]["content"]
    assert "Write in English only" in user_content
    assert "Output ONLY the image generation prompt, in English." in user_content


async def test_generate_image_prompt_includes_recent_exchanges(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    messages = [
        {"role": "user", "content": "Tell me about the moonlit tavern"},
        {"role": "assistant", "content": "The tavern glows with candlelight."},
    ]
    await svc._generate_image_prompt(conn, char, messages, "scene")
    user_content = conn.received[0][0]["content"]
    assert "moonlit tavern" in user_content
    assert "candlelight" in user_content


async def test_generate_image_prompt_no_messages_uses_placeholder(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    await svc._generate_image_prompt(conn, char, [], "scene")
    user_content = conn.received[0][0]["content"]
    assert "(no prior exchanges)" in user_content


async def test_generate_image_prompt_excludes_system_messages(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    messages = [
        {"role": "system", "content": "SECRET_SYSTEM_CONTENT"},
        {"role": "user", "content": "hello"},
    ]
    await svc._generate_image_prompt(conn, char, messages, "scene")
    user_content = conn.received[0][0]["content"]
    assert "SECRET_SYSTEM_CONTENT" not in user_content


async def test_generate_image_prompt_limits_context_to_max_messages(tmp_path):
    conn = _CapturingText()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg_{i}"}
        for i in range(10)
    ]
    await svc._generate_image_prompt(conn, char, messages, "scene")
    user_content = conn.received[0][0]["content"]
    # last 6 messages (indices 4-9) must appear; earlier ones must not
    assert "msg_4" in user_content
    assert "msg_9" in user_content
    assert "msg_0" not in user_content
    assert "msg_3" not in user_content


async def test_generate_image_prompt_empty_llm_result_falls_back_to_raw(tmp_path):
    conn = _CapturingText(response="   ")
    char_svc, conv_svc, svc = make_chat_service(tmp_path, conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    result = await svc._generate_image_prompt(conn, char, [], "original prompt")
    assert result.prompt == "original prompt"


# ---------------------------------------------------------------------------
# _generate_image_prompt — tool-calling path
# ---------------------------------------------------------------------------

async def test_stream_image_tool_call_uses_llm_enhanced_prompt(tmp_path):
    """Image prompt is LLM-enhanced also when triggered via the tool-calling path."""

    class _ToolCallText:
        connector_type = "text"
        supports_tool_calling = True

        async def stream_chat_completion_with_tools(self, messages, tools, **kw):
            yield {"type": "tool_call", "name": "generate_image", "arguments": {"prompt": "elf girl forest"}}

        async def stream_chat_completion(self, messages, **kw):
            yield "llm enhanced tool call prompt"

        async def test_connection(self):
            return {}

    img_conn = _RecordingImage()
    char_svc, conv_svc, svc = make_chat_service(tmp_path, _ToolCallText(), img_conn)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Show me the scene"))
    assert any(e["type"] == "image_complete" for e in events)
    assert img_conn.last_prompt == "llm enhanced tool call prompt"


async def test_stream_schedule_tool_creates_runtime_schedule(tmp_path):
    class _ToolCallText:
        connector_type = "text"
        supports_tool_calling = True

        async def stream_chat_completion_with_tools(self, messages, tools, **kw):
            yield {
                "type": "tool_call",
                "name": "schedule_proactive_message",
                "arguments": {
                    "id": "followup",
                    "type": "after_delay",
                    "delay_minutes": 60,
                    "instruction": "Check back naturally.",
                },
            }
            yield {"type": "token", "content": "Noted."}

        async def test_connection(self):
            return {}

    char_svc, conv_svc, svc = make_chat_service(tmp_path, _ToolCallText(), None)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    conv = conv_svc.create_conversation(char.id)
    await collect(svc.stream_chat(conv.id, "Remind me later"))

    from aubergeRP.services.schedule_instance_service import ScheduleInstanceService

    rows = ScheduleInstanceService(tmp_path).list_for_conversation(conv.id)
    assert any(r.schedule_def_id == "followup" and r.origin == "character-tool" for r in rows)


async def test_stream_schedule_tool_cancel_deletes_runtime_schedule(tmp_path):
    class _ToolCallText:
        connector_type = "text"
        supports_tool_calling = True

        async def stream_chat_completion_with_tools(self, messages, tools, **kw):
            yield {
                "type": "tool_call",
                "name": "cancel_scheduled_message",
                "arguments": {"id": "followup"},
            }
            yield {"type": "token", "content": "Cancelled."}

        async def test_connection(self):
            return {}

    char_svc, conv_svc, svc = make_chat_service(tmp_path, _ToolCallText(), None)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    conv = conv_svc.create_conversation(char.id)
    from aubergeRP.models.character import ScheduleDefinition
    from aubergeRP.services.schedule_instance_service import ScheduleInstanceService

    sched_svc = ScheduleInstanceService(tmp_path)
    sched_svc.get_or_create(
        defn=ScheduleDefinition(
            id="followup",
            enabled=True,
            type="after_delay",
            delay_minutes=60,
            instruction="Check back naturally.",
        ),
        character_id=char.id,
        conversation_id=conv.id,
        channel="web",
        channel_instance_id="web",
        external_user_id="u1",
        external_chat_id="",
        timezone="UTC",
        origin="character-tool",
    )

    await collect(svc.stream_chat(conv.id, "cancel it"))
    rows = sched_svc.list_for_conversation(conv.id)
    assert all(r.schedule_def_id != "followup" for r in rows)


async def test_stream_chat_rebases_event_triggers_with_persisted_user_timestamp(tmp_path, monkeypatch):
    char_svc, conv_svc, svc = make_chat_service(tmp_path, _FakeText(["Hello"]), None)
    char = char_svc.create_character(CharacterData(name="Elara", description="An elven ranger."))
    conv = conv_svc.create_conversation(char.id)

    captured: dict[str, Any] = {}

    def _capture_rebase(self, conversation_id: str, *, user_message_at=None):
        captured["conversation_id"] = conversation_id
        captured["user_message_at"] = user_message_at
        return 0

    monkeypatch.setattr(
        "aubergeRP.services.schedule_instance_service.ScheduleInstanceService.rebase_event_triggers_on_user_message",
        _capture_rebase,
    )

    await collect(svc.stream_chat(conv.id, "Hi there"))

    reloaded = conv_svc.get_conversation(conv.id)
    last_user_message = [m for m in reloaded.messages if m.role == "user"][-1]
    assert captured["conversation_id"] == conv.id
    assert captured["user_message_at"] == last_user_message.timestamp


# ---------------------------------------------------------------------------
# No image connector — conversation-only mode
# ---------------------------------------------------------------------------

async def test_no_image_connector_plain_text_conversation_works(tmp_path):
    """A plain text conversation must succeed when no image connector is set."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["Hello", " there!"]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))

    # No errors at all
    assert not any(e["type"] == "error" for e in events)
    # Text tokens are delivered
    tokens = [e for e in events if e["type"] == "token"]
    assert len(tokens) == 2
    assert tokens[0]["content"] == "Hello"
    assert tokens[1]["content"] == " there!"
    # Stream ends with done
    assert events[-1]["type"] == "done"


async def test_no_image_connector_conversation_saves_messages(tmp_path):
    """Messages must be persisted even when no image connector is configured."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["Nice to meet you."]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    await collect(svc.stream_chat(conv.id, "Hello"))

    reloaded = conv_svc.get_conversation(conv.id)
    roles = [m.role for m in reloaded.messages]
    assert "user" in roles
    assert "assistant" in roles
    assistant_msg = next(m for m in reloaded.messages if m.role == "assistant")
    assert assistant_msg.content == "Nice to meet you."
    assert assistant_msg.images == []


async def test_no_image_connector_no_image_generation_called(tmp_path):
    """No image generation must be attempted when there is no image connector."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["Just text, no images."]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Tell me something"))

    # No image events should be emitted
    image_event_types = {"image_start", "image_complete", "image_failed", "image_progress"}
    assert not any(e["type"] in image_event_types for e in events)


async def test_no_image_connector_multiple_turns(tmp_path):
    """Multiple conversation turns must work correctly with no image connector."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["reply"]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)

    # First turn
    events1 = await collect(svc.stream_chat(conv.id, "First message"))
    assert events1[-1]["type"] == "done"

    # Second turn — service must re-create connector each time
    svc2 = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(_FakeText(["second reply"]), None),
        images_dir=tmp_path / "images",
    )
    events2 = await collect(svc2.stream_chat(conv.id, "Second message"))
    assert not any(e["type"] == "error" for e in events2)
    assert events2[-1]["type"] == "done"

    reloaded = conv_svc.get_conversation(conv.id)
    assert len([m for m in reloaded.messages if m.role == "assistant"]) == 2


# ---------------------------------------------------------------------------
# generate_scene_image
# ---------------------------------------------------------------------------

async def test_generate_scene_image_emits_start_and_complete(tmp_path):
    """generate_scene_image yields image_start then image_complete on success."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["an enhanced scene prompt"]),
        image_conn=_FakeImage(b"SCENEIMG"),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.generate_scene_image(conv.id))
    assert any(e["type"] == "image_start" for e in events)
    assert any(e["type"] == "image_complete" for e in events)


async def test_generate_scene_image_no_image_connector(tmp_path):
    """generate_scene_image yields image_failed when no image connector is configured."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["prompt"]),
        image_conn=None,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.generate_scene_image(conv.id))
    assert any(e["type"] == "image_failed" for e in events)
    failed = next(e for e in events if e["type"] == "image_failed")
    assert "connector" in failed["detail"].lower()


async def test_generate_scene_image_invalid_conversation(tmp_path):
    """generate_scene_image yields image_failed for a non-existent conversation."""
    _, _, svc = make_chat_service(tmp_path, image_conn=_FakeImage())
    events = await collect(svc.generate_scene_image("nonexistent-conv"))
    assert events[0]["type"] == "image_failed"


async def test_generate_scene_image_uses_image_connector(tmp_path):
    """generate_scene_image calls the image connector and saves an image to disk."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=_FakeText(["scene description"]),
        image_conn=_FakeImage(b"BYTES"),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.generate_scene_image(conv.id))
    complete = next(e for e in events if e["type"] == "image_complete")
    filename = complete["image_url"].split("/")[-1]
    # Image was written to disk
    images_dir = tmp_path / "images"
    assert any(images_dir.rglob(filename))


async def test_generate_scene_image_works_without_text_connector(tmp_path):
    """generate_scene_image should still yield image events with no text connector."""
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path,
        text_conn=None,
        image_conn=_FakeImage(b"IMG"),
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.generate_scene_image(conv.id))
    # Should have image_start + image_complete (no text connector means no prompt enhancement)
    assert any(e["type"] == "image_start" for e in events)
    assert any(e["type"] == "image_complete" for e in events)


# ---------------------------------------------------------------------------
# stream_chat — empty response warning (reasoning model protection)
# ---------------------------------------------------------------------------

async def test_stream_empty_response_yields_warning(tmp_path):
    """When the LLM returns no text and no images, a warning event is emitted."""
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText([]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    warnings = [e for e in events if e["type"] == "warning"]
    assert len(warnings) == 1
    assert "reasoning" in warnings[0]["detail"].lower() or "empty" in warnings[0]["detail"].lower()


async def test_stream_empty_response_still_ends_with_done(tmp_path):
    """Even when the response is empty, the stream must end with a done event."""
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText([]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert events[-1]["type"] == "done"


async def test_stream_non_empty_response_no_warning(tmp_path):
    """No warning is emitted when the LLM returns normal content."""
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=_FakeText(["Hello!"]))
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)
    events = await collect(svc.stream_chat(conv.id, "Hi"))
    assert not any(e["type"] == "warning" for e in events)


# ---------------------------------------------------------------------------
# Automatic retry with backoff
# ---------------------------------------------------------------------------

class _FlakyText:
    """Fails the first *fail_times* calls, then streams normally.

    ``max_retries`` mirrors the connector config field the real connectors
    expose; ChatService reads it to size its retry schedule.
    """
    connector_type = "text"

    def __init__(
        self,
        fail_times: int,
        token: str = "Hello",
        max_retries: int = 4,
        error: Exception | None = None,
    ) -> None:
        self._fail_times = fail_times
        self._token = token
        self._error = error or httpx.ConnectError("connection refused")
        self.max_retries = max_retries
        self.calls = 0

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error
        yield self._token

    async def test_connection(self) -> dict:
        return {}


@pytest.fixture
def retry_delays(monkeypatch):
    """Record the backoff waits instead of sleeping through them."""
    from aubergeRP.services import chat_service
    from aubergeRP.utils import retry
    sleeps: list[float] = []

    async def _sleep(delay):
        sleeps.append(delay)

    # conftest zeroes the delays globally; restore them so the schedule is visible.
    monkeypatch.setattr(retry, "RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr(retry, "RETRY_MAX_DELAY", 30.0)
    monkeypatch.setattr(chat_service.asyncio, "sleep", _sleep)
    return sleeps


async def test_generate_reply_retries_until_success(tmp_path, retry_delays):
    text = _FlakyText(fail_times=2)
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    result = await svc.generate_reply(conv.id, "Hi")

    assert result.text == "Hello"
    assert text.calls == 3
    assert retry_delays == [1.0, 2.0]
    # The user turn is stored exactly once despite the retries.
    reloaded = conv_svc.get_conversation(conv.id)
    assert [m.role for m in reloaded.messages] == ["user", "assistant"]


async def test_generate_reply_gives_up_after_five_attempts(tmp_path, retry_delays):
    text = _FlakyText(fail_times=99)  # max_retries=4 → 5 attempts total
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    assert text.calls == 5
    assert retry_delays == [1.0, 2.0, 4.0, 8.0]
    assert conv_svc.get_conversation(conv.id).messages == []


async def test_stream_chat_retries_and_emits_retry_events(tmp_path, retry_delays):
    text = _FlakyText(fail_times=1)
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Hi"))

    assert not any(e["type"] == "error" for e in events)
    assert [e for e in events if e["type"] == "retry"] == [
        {"type": "retry", "attempt": 1, "delay": 1.0}
    ]
    assert any(e["type"] == "done" for e in events)


async def test_retry_count_follows_connector_max_retries(tmp_path, retry_delays):
    """The connector's own max_retries sizes the retry schedule."""
    text = _FlakyText(fail_times=99, max_retries=2)
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    assert text.calls == 3
    assert retry_delays == [1.0, 2.0]


async def test_no_retry_when_max_retries_is_zero(tmp_path, retry_delays):
    text = _FlakyText(fail_times=99, max_retries=0)
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    assert text.calls == 1
    assert retry_delays == []


async def test_no_retry_on_definitive_error(tmp_path, retry_delays):
    """An invalid API key (HTTP 401) fails immediately instead of being retried."""
    request = httpx.Request("POST", "http://llm.test/v1/chat/completions")
    unauthorized = httpx.HTTPStatusError(
        "unauthorized", request=request, response=httpx.Response(401, request=request)
    )
    text = _FlakyText(fail_times=99, max_retries=4, error=unauthorized)
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=text)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    with pytest.raises(ChatGenerationError):
        await svc.generate_reply(conv.id, "Hi")

    assert text.calls == 1
    assert retry_delays == []


async def test_no_retry_when_connector_missing(tmp_path, retry_delays):
    """Configuration errors surface immediately instead of being retried."""
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn=None)
    char = char_svc.create_character(CharacterData(name="X", description="Y", first_mes=""))
    conv = conv_svc.create_conversation(char.id)

    events = await collect(svc.stream_chat(conv.id, "Hi"))

    assert events[0]["type"] == "error"
    assert retry_delays == []


# ---------------------------------------------------------------------------
# _handle_image — character prefix, negative prompt and audit trail
# ---------------------------------------------------------------------------

class _FullRecordingImage:
    """Image connector recording both the positive and the negative prompt."""

    connector_type = "image"

    def __init__(self) -> None:
        self.last_prompt = ""
        self.last_negative = ""

    async def generate_image(self, prompt, negative_prompt="", **kw) -> bytes:
        self.last_prompt = prompt
        self.last_negative = negative_prompt
        return b"PNG"

    async def generate_image_with_progress(self, prompt, negative_prompt="", **kw):
        self.last_prompt = prompt
        self.last_negative = negative_prompt
        yield {"type": "complete", "bytes": b"PNG"}

    async def test_connection(self) -> dict:
        return {}


def _character_with_image_settings(char_svc, prefix: str, negative: str):
    return char_svc.create_character(
        CharacterData(
            name="Elara",
            description="An elven ranger.",
            extensions={
                "aubergerp": {
                    "image_prompt_prefix": prefix,
                    "negative_prompt": negative,
                }
            },
        )
    )


async def test_handle_image_applies_character_prefix_and_negative(tmp_path):
    img_conn = _FullRecordingImage()
    text_conn = _FakeText(["a moonlit clearing"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn, img_conn)
    char = _character_with_image_settings(char_svc, "oil painting,", "blurry, watermark")

    events = await collect(
        svc._handle_image(char, "gen-1", "forest", text_conn, [{"role": "user", "content": "hi"}])
    )

    assert img_conn.last_prompt == "oil painting, a moonlit clearing"
    assert img_conn.last_negative == "blurry, watermark"
    complete = next(e for e in events if e["type"] == "image_complete")
    details = complete["details"]
    assert details["raw_prompt"] == "forest"
    assert details["llm_output_prompt"] == "a moonlit clearing"
    assert details["prompt_prefix"] == "oil painting,"
    assert details["negative_prompt"] == "blurry, watermark"
    assert details["prompt"] == "oil painting, a moonlit clearing"
    assert "Elara" in details["llm_input_prompt"]
    assert details["connector_name"]


async def test_handle_image_without_character_settings_leaves_prompt_untouched(tmp_path):
    img_conn = _FullRecordingImage()
    text_conn = _FakeText(["a moonlit clearing"])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn, img_conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))

    events = await collect(svc._handle_image(char, "gen-1", "forest", text_conn, []))

    assert img_conn.last_prompt == "a moonlit clearing"
    assert img_conn.last_negative == ""
    details = next(e for e in events if e["type"] == "image_complete")["details"]
    assert details["prompt_prefix"] == ""
    assert details["negative_prompt"] == ""


async def test_handle_image_strips_reasoning_and_quotes_from_llm_output(tmp_path):
    img_conn = _FullRecordingImage()
    text_conn = _FakeText(['<think>Let me picture it.</think> "a moonlit clearing"'])
    char_svc, conv_svc, svc = make_chat_service(tmp_path, text_conn, img_conn)
    char = char_svc.create_character(CharacterData(name="X", description="Y"))

    await collect(svc._handle_image(char, "gen-1", "forest", text_conn, []))

    assert img_conn.last_prompt == "a moonlit clearing"


async def test_handle_image_records_error_when_prompt_llm_fails(tmp_path):
    """A broken text_utility connector must not degrade images silently."""
    get_registry().reset()
    img_conn = _FullRecordingImage()
    char_svc, conv_svc, svc = make_chat_service(
        tmp_path, _FailText(), img_conn, role_conns={"text_utility": _FailText()}
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))

    events = await collect(svc._handle_image(char, "gen-1", "forest", _FailText(), []))

    # The image is still produced, from the raw keywords.
    assert any(e["type"] == "image_complete" for e in events)
    assert img_conn.last_prompt == "forest"
    assert get_registry().list_errors(component="image")


async def test_image_prompt_call_is_recorded_in_statistics(tmp_path):
    """The prompt-building round-trip is billed, so it must be counted."""
    char_svc, conv_svc = make_services(tmp_path)
    stats = StatisticsService(data_dir=tmp_path)
    text_conn = _FakeText(["a moonlit clearing"])
    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(text_conn, _FullRecordingImage()),
        images_dir=tmp_path / "images",
        statistics_service=stats,
    )
    char = char_svc.create_character(CharacterData(name="X", description="Y"))
    conv = conv_svc.create_conversation(char.id)

    await collect(
        svc._handle_image(char, "gen-1", "forest", text_conn, [], conversation_id=conv.id)
    )

    from sqlmodel import Session, select

    from aubergeRP.database import get_engine
    from aubergeRP.db_models import LLMCallStatRow

    with Session(get_engine(tmp_path)) as session:
        rows = list(session.exec(select(LLMCallStatRow)).all())
    assert any(r.generation_type == "image_prompt" for r in rows)
