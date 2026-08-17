"""Focused tests for RP-continuity summarization.

Covers:
  1. Summarization triggers near the context-window threshold.
  2. Recent messages remain verbatim after summarization.
  3. The summarization prompt includes RP continuity instructions.
  4. The summary is reinjected into future context as a system message.
  5. Summarization failure preserves the original conversation history.
  6. Web and Telegram both use the same summarization path (ChatService /
     SummaryService).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aubergeRP.models.character import CharacterData
from aubergeRP.services.character_service import CharacterService
from aubergeRP.services.chat_service import ChatService
from aubergeRP.services.conversation_service import ConversationService
from aubergeRP.services.prompt_service import get_prompt
from aubergeRP.services.summarization_service import (
    _MIN_RECENT_MESSAGES,
    _build_summary_prompt,
)
from aubergeRP.services.summary_service import SummaryService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_services(tmp_path: Path):
    char_svc = CharacterService(data_dir=tmp_path)
    conv_svc = ConversationService(data_dir=tmp_path, character_service=char_svc)
    return char_svc, conv_svc


def _manager(text_conn) -> MagicMock:
    m = MagicMock()
    m.get_active_text_connector.return_value = text_conn
    # Every per-task role resolves to the same connector by default.
    m.get_text_connector.side_effect = lambda role="text": text_conn

    def _active_id_for_type(connector_type: str) -> str:
        return "text-active" if connector_type == "text" else ""

    m.get_active_id_for_type.side_effect = _active_id_for_type

    def _get_connector(connector_id: str) -> MagicMock:
        if connector_id == "text-active":
            inst = MagicMock()
            inst.config = {"nsfw": False}
            return inst
        raise KeyError(connector_id)

    m.get_connector.side_effect = _get_connector
    return m


def _conversation(tmp_path: Path, turns: int, *, filler: str = "x" * 300):
    """Create a character + conversation with *turns* user/assistant pairs."""
    char_svc, conv_svc = _make_services(tmp_path)
    char = char_svc.create_character(CharacterData(name="Aria", description="A healer."))
    conv = conv_svc.create_conversation(char.id)
    for i in range(turns):
        conv_svc.append_message(conv.id, "user", f"user-{i} {filler}")
        conv_svc.append_message(conv.id, "assistant", f"assistant-{i} {filler}")
    return char_svc, conv_svc, char, conv_svc.get_conversation(conv.id)


async def _build(tmp_path: Path, conv, char, connector, *, context_window: int,
                 threshold: float, max_tokens: int = 64) -> list[dict[str, str]]:
    return await SummaryService(tmp_path).build_prompt_within_budget(
        conv,
        connector=connector,
        context_window=context_window,
        threshold=threshold,
        max_tokens=max_tokens,
        char=char,
        user_name="Traveler",
    )


class _SummaryConnector:
    """Connector that returns a preset summary on first call, reply on later calls."""

    connector_type = "text"
    supports_tool_calling = False

    def __init__(self, summary: str = "SUMMARY", reply: str = "Reply.") -> None:
        self._call = 0
        self._summary = summary
        self._reply = reply

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        self._call += 1
        text = self._summary if self._call == 1 else self._reply
        for word in text.split():
            yield word + " "

    async def test_connection(self) -> dict:
        return {"connected": True}


class _FailingConnector:
    """Connector that always raises on stream_chat_completion."""

    connector_type = "text"
    supports_tool_calling = False

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        raise RuntimeError("LLM unavailable")
        if False:  # noqa: PIE790
            yield ""

    async def test_connection(self) -> dict:
        return {"connected": True}


class _CountingConnector:
    """Records how many times it was asked to summarize."""

    connector_type = "text"
    supports_tool_calling = False

    def __init__(self, text: str = "condensed history") -> None:
        self.calls = 0
        self._text = text

    async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
        self.calls += 1
        yield self._text


# ---------------------------------------------------------------------------
# 1. Summarization triggers near context threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarization_triggers_near_threshold(tmp_path):
    """When token count exceeds threshold × context_window, the LLM is called."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=4)
    connector = _CountingConnector()

    result = await _build(
        tmp_path, conv, char, connector, context_window=600, threshold=0.75
    )

    assert connector.calls == 1, "LLM must be called when over threshold"
    assert len(result) < len(conv.messages) + 1, "Result should be compressed"


@pytest.mark.asyncio
async def test_no_summarization_under_threshold(tmp_path):
    """Well below the threshold, no summarization call happens at all."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=1, filler="hi")
    connector = _CountingConnector()

    await _build(tmp_path, conv, char, connector, context_window=40960, threshold=0.75)

    assert connector.calls == 0
    assert SummaryService(tmp_path).get_latest(conv.id) is None


@pytest.mark.asyncio
async def test_summary_is_reused_without_a_second_llm_call(tmp_path):
    """The stored summary is reused on the next turn instead of being redone.

    This is the regression that motivated persistence: the summary used to be
    recomputed from the full history on every single turn.
    """
    _cs, _vs, char, conv = _conversation(tmp_path, turns=4)
    connector = _CountingConnector()

    await _build(tmp_path, conv, char, connector, context_window=600, threshold=0.75)
    assert connector.calls == 1

    # Same conversation, next turn: the prompt now fits thanks to the summary.
    second = await _build(
        tmp_path, conv, char, connector, context_window=600, threshold=0.75
    )
    assert connector.calls == 1, "The stored summary must be reused, not recomputed"
    assert any("[Summary" in m["content"] for m in second)


@pytest.mark.asyncio
async def test_next_summary_builds_on_the_previous_one(tmp_path):
    """A follow-up summary is fed the previous summary, not the whole history."""
    char_svc, conv_svc, char, conv = _conversation(tmp_path, turns=4)
    svc = SummaryService(tmp_path)

    class _Recorder:
        connector_type = "text"
        supports_tool_calling = False

        def __init__(self) -> None:
            self.excerpts: list[str] = []
            self.n = 0

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            self.n += 1
            self.excerpts.append(messages[-1]["content"])
            yield f"SUMMARY-{self.n}"

    connector = _Recorder()
    first = await svc.summarize_now(conv, connector)
    assert first is not None and first.based_on_summary_id == ""

    for i in range(4):
        conv_svc.append_message(conv.id, "user", f"later-{i} " + "y" * 300)
    conv = conv_svc.get_conversation(conv.id)

    second = await svc.summarize_now(conv, connector)
    assert second is not None
    assert second.based_on_summary_id == first.id
    assert "SUMMARY-1" in connector.excerpts[1], "The previous summary must be carried over"
    assert "user-0" not in connector.excerpts[1], "Already-summarized turns are not re-read"
    assert second.covers_message_count > first.covers_message_count


# ---------------------------------------------------------------------------
# 2. Recent messages remain verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_messages_verbatim_after_summarization(tmp_path):
    """The _MIN_RECENT_MESSAGES most recent messages must survive intact."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=6)
    recent = [m.content for m in conv.messages[-_MIN_RECENT_MESSAGES:]]

    result = await _build(
        tmp_path, conv, char, _CountingConnector(), context_window=200, threshold=0.1
    )

    contents = [m.get("content", "") for m in result]
    for c in recent:
        assert c in contents, f"Recent message '{c[:20]}…' must be preserved verbatim"


@pytest.mark.asyncio
async def test_min_recent_messages_all_preserved(tmp_path):
    """Even for a huge conversation, at least _MIN_RECENT_MESSAGES are kept."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=10, filler="a" * 500)

    result = await _build(
        tmp_path, conv, char, _CountingConnector(), context_window=100, threshold=0.1
    )

    non_system = [m for m in result if m.get("role") != "system"]
    assert len(non_system) >= _MIN_RECENT_MESSAGES


# ---------------------------------------------------------------------------
# 3. RP continuity facts are included in the summarization prompt
# ---------------------------------------------------------------------------

RP_CONTINUITY_KEYWORDS = [
    "RELATIONSHIP",
    "PROMISES",
    "CONFLICTS",
    "ONGOING",
    "UNRESOLVED",
]


def test_summarization_system_prompt_contains_rp_keywords():
    """The system prompt must instruct the model to preserve RP continuity facts."""
    system_prompt = get_prompt("summarization_system")
    for keyword in RP_CONTINUITY_KEYWORDS:
        assert keyword in system_prompt, (
            f"summarization_system prompt must mention '{keyword}'"
        )


def test_summarization_user_prompt_mentions_rp_continuity():
    """The user prompt template must reference RP continuity."""
    user_prompt = get_prompt("summarization_user")
    rp_terms = ["relationship", "promises", "continuity", "conflict", "thread"]
    assert any(t in user_prompt.lower() for t in rp_terms), (
        "summarization_user prompt must reference RP continuity concepts"
    )


def test_build_summary_prompt_includes_excerpt():
    """_build_summary_prompt must embed the full conversation excerpt."""
    msgs = [
        {"role": "user", "content": "Hello there"},
        {"role": "assistant", "content": "Greetings, traveler"},
    ]
    prompt = _build_summary_prompt(msgs)
    combined = " ".join(m.get("content", "") for m in prompt)
    assert "Hello there" in combined
    assert "Greetings, traveler" in combined


def test_build_summary_prompt_includes_previous_summary():
    """A follow-up summary must be given the summary it extends."""
    prompt = _build_summary_prompt(
        [{"role": "user", "content": "new turn"}], "earlier events"
    )
    combined = " ".join(m.get("content", "") for m in prompt)
    assert "earlier events" in combined
    assert "new turn" in combined


def test_build_summary_prompt_uses_system_role_first():
    """_build_summary_prompt must start with a system message."""
    prompt = _build_summary_prompt([{"role": "user", "content": "hi"}])
    assert prompt[0]["role"] == "system"
    assert len(prompt) >= 2


# ---------------------------------------------------------------------------
# 4. Summary is reinjected into future context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_injected_as_system_message(tmp_path):
    """After summarization the summary appears as a system message in the result."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=4)
    connector = _CountingConnector("RELATIONSHIP: friends. EVENTS: met at inn.")

    result = await _build(
        tmp_path, conv, char, connector, context_window=200, threshold=0.1
    )

    summary_msgs = [
        m for m in result
        if m.get("role") == "system" and "[Summary" in m.get("content", "")
    ]
    assert len(summary_msgs) == 1, "Exactly one summary system message must be present"
    assert "RELATIONSHIP" in summary_msgs[0]["content"]


@pytest.mark.asyncio
async def test_summary_placed_after_system_header_and_before_history(tmp_path):
    """The summary sits between the system header and the recent messages."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=4)
    marker = conv.messages[-1].content

    result = await _build(
        tmp_path, conv, char, _CountingConnector(), context_window=100, threshold=0.1
    )

    assert result[0]["role"] == "system"
    assert "Aria" in result[0]["content"], "Character system block must come first"
    summary_idx = next(
        i for i, m in enumerate(result) if "[Summary" in m.get("content", "")
    )
    recent_idx = next(i for i, m in enumerate(result) if marker in m.get("content", ""))
    assert 0 < summary_idx < recent_idx


# ---------------------------------------------------------------------------
# 5. Summarization failure preserves usable conversation history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_preserves_all_history(tmp_path):
    """On failure nothing is dropped and no summary is stored."""
    _cs, _vs, char, conv = _conversation(tmp_path, turns=3)

    result = await _build(
        tmp_path, conv, char, _FailingConnector(), context_window=100, threshold=0.1
    )

    contents = [m.get("content", "") for m in result]
    for msg in conv.messages:
        assert msg.content in contents, "No message may be lost when summarization fails"
    assert SummaryService(tmp_path).get_latest(conv.id) is None


@pytest.mark.asyncio
async def test_empty_summary_response_stores_nothing(tmp_path):
    """An empty summary is not persisted — it would erase the history for nothing."""

    class _EmptyConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            if False:  # noqa: PIE790
                yield ""

    _cs, _vs, char, conv = _conversation(tmp_path, turns=4)

    result = await _build(
        tmp_path, conv, char, _EmptyConnector(), context_window=200, threshold=0.1
    )

    assert SummaryService(tmp_path).get_latest(conv.id) is None
    assert not any("[Summary" in m.get("content", "") for m in result)


@pytest.mark.asyncio
async def test_stale_summary_is_dropped(tmp_path):
    """If the message a summary points at is gone, the chain is invalidated."""
    _cs, conv_svc, char, conv = _conversation(tmp_path, turns=4)
    svc = SummaryService(tmp_path)
    row = await svc.summarize_now(conv, _CountingConnector())
    assert row is not None

    conv_svc.delete_message(conv.id, row.covers_until_message_id)
    conv = conv_svc.get_conversation(conv.id)

    summary_text, history = svc.split_history(conv, svc.get_latest(conv.id))
    assert summary_text == ""
    assert len(history) == len(conv.messages)
    assert svc.get_latest(conv.id) is None


# ---------------------------------------------------------------------------
# 6. Web and Telegram share the same summarization system
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_service_persists_a_summary(tmp_path):
    """ChatService.generate_reply summarizes through SummaryService and stores it."""
    connector = _SummaryConnector(summary="RELATIONSHIP: allies.", reply="Reply.")
    char_svc, conv_svc = _make_services(tmp_path)
    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(connector),
        images_dir=tmp_path / "images",
        context_window=50,
        summarization_threshold=0.1,
    )
    char = char_svc.create_character(CharacterData(name="B", description="Bot."))
    conv = conv_svc.create_conversation(char.id)
    for _ in range(4):
        conv_svc.append_message(conv.id, "user", "hello " * 20)
        conv_svc.append_message(conv.id, "assistant", "world " * 20)

    from aubergeRP.services.chat_service import GenerationOptions

    await svc.generate_reply(
        conversation_id=conv.id,
        content="Hello",
        options=GenerationOptions(),
    )

    stored = SummaryService(tmp_path).get_latest(conv.id)
    assert stored is not None, "generate_reply must persist the summary it produced"
    assert "allies" in stored.content


def test_telegram_uses_chat_service_module():
    """TelegramRuntimeManager._generate imports ChatService from the chat_service module."""
    import ast
    import inspect

    import aubergeRP.services.telegram_runtime_manager as trm_mod

    source = inspect.getsource(trm_mod)
    tree = ast.parse(source)

    # Look for: from ..services.chat_service import ChatService (any relative level)
    import_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            if "chat_service" in module and "ChatService" in names:
                import_found = True
                break

    assert import_found, (
        "TelegramRuntimeManager must import ChatService from a chat_service module"
    )


@pytest.mark.asyncio
async def test_web_and_telegram_same_summarization_path(tmp_path):
    """Both web and Telegram go through ChatService, hence through SummaryService."""
    connector = _SummaryConnector(
        summary="RELATIONSHIP: allies.",
        reply="All good.",
    )

    char_svc, conv_svc = _make_services(tmp_path)
    svc = ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=_manager(connector),
        images_dir=tmp_path / "images",
        context_window=50,
        summarization_threshold=0.1,
    )

    char = char_svc.create_character(CharacterData(name="Aria", description="A healer."))
    conv = conv_svc.create_conversation(char.id)

    # Add history to force summarization
    for _ in range(5):
        conv_svc.append_message(conv.id, "user", "hello " * 20)
        conv_svc.append_message(conv.id, "assistant", "world " * 20)

    from aubergeRP.services.chat_service import GenerationOptions

    result = await svc.generate_reply(
        conversation_id=conv.id,
        content="How are you?",
        options=GenerationOptions(),
    )

    assert result.text.strip() != "", "Must get a reply"
    # LLM called at least twice: once for summarization, once for reply
    assert connector._call >= 2
