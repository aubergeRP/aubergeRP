"""Focused tests for RP-continuity summarization.

Covers:
  1. Summarization triggers near the context-window threshold.
  2. Recent messages remain verbatim after summarization.
  3. The summarization prompt includes RP continuity instructions.
  4. The summary is reinjected into future context as a system message.
  5. Summarization failure preserves the original conversation history.
  6. Web and Telegram both use the same summarization path (ChatService /
     maybe_summarize).
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
    maybe_summarize,
)

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


# ---------------------------------------------------------------------------
# 1. Summarization triggers near context threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarization_triggers_near_threshold():
    """When token count exceeds threshold × context_window, LLM is called."""
    called = {"n": 0}

    class _CountingConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            called["n"] += 1
            yield "summary"

    msgs = [
        {"role": "system", "content": "sys"},
        # Six old messages long enough to push over budget
        *[{"role": "user", "content": "x" * 300} for _ in range(3)],
        *[{"role": "assistant", "content": "y" * 300} for _ in range(3)],
        {"role": "user", "content": "recent"},
    ]

    result = await maybe_summarize(
        msgs, _CountingConnector(), context_window=600, threshold=0.75
    )

    assert called["n"] == 1, "LLM must be called when over threshold"
    assert len(result) < len(msgs), "Result should be compressed"


@pytest.mark.asyncio
async def test_no_summarization_under_threshold():
    """When token count is well below threshold, messages are returned unchanged."""

    class _NeverCalledConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            raise AssertionError("Should not be called")
            if False:
                yield ""

    msgs = [{"role": "user", "content": "hi"}]
    result = await maybe_summarize(
        msgs, _NeverCalledConnector(), context_window=4096, threshold=0.75
    )
    assert result is msgs


# ---------------------------------------------------------------------------
# 2. Recent messages remain verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_messages_verbatim_after_summarization():
    """The _MIN_RECENT_MESSAGES most recent messages must survive summarization intact."""

    class _QuickSummary:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            yield "condensed history"

    recent_contents = [f"recent-msg-{i}" for i in range(_MIN_RECENT_MESSAGES)]
    old_contents = ["old-msg-" + "x" * 200 for _ in range(6)]

    msgs = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": c} for c in old_contents]
        + [{"role": "user", "content": c} for c in recent_contents]
    )

    result = await maybe_summarize(
        msgs, _QuickSummary(), context_window=200, threshold=0.1
    )

    result_contents = [m.get("content", "") for m in result]
    for c in recent_contents:
        assert c in result_contents, f"Recent message '{c}' must be preserved verbatim"


@pytest.mark.asyncio
async def test_min_recent_messages_all_preserved():
    """Even if conversation is huge, at least _MIN_RECENT_MESSAGES are kept."""

    class _QuickSummary:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            yield "summary"

    # Build a large conversation where every message is expensive
    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "a" * 500}
        for i in range(20)
    ]

    result = await maybe_summarize(
        msgs, _QuickSummary(), context_window=100, threshold=0.1
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


def test_build_summary_prompt_uses_system_role_first():
    """_build_summary_prompt must start with a system message."""
    prompt = _build_summary_prompt([{"role": "user", "content": "hi"}])
    assert prompt[0]["role"] == "system"
    assert len(prompt) >= 2


# ---------------------------------------------------------------------------
# 4. Summary is reinjected into future context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_injected_as_system_message():
    """After summarization the summary appears as a system message in the result."""

    class _SumConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            yield "RELATIONSHIP: friends. EVENTS: met at inn."

    msgs = (
        [{"role": "system", "content": "You are Aria."}]
        + [{"role": "user", "content": "a" * 300} for _ in range(6)]
        + [{"role": "user", "content": "latest message"}]
    )

    result = await maybe_summarize(
        msgs, _SumConnector(), context_window=200, threshold=0.1
    )

    summary_msgs = [
        m for m in result
        if m.get("role") == "system" and "[Summary" in m.get("content", "")
    ]
    assert len(summary_msgs) == 1, "Exactly one summary system message must be present"
    assert "RELATIONSHIP" in summary_msgs[0]["content"] or "friends" in summary_msgs[0]["content"]


@pytest.mark.asyncio
async def test_summary_placed_after_system_header():
    """The summary system message must be placed after the original system messages."""

    class _SumConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            yield "some summary"

    msgs = (
        [{"role": "system", "content": "Original system."}]
        + [{"role": "user", "content": "x" * 400} for _ in range(6)]
        + [{"role": "user", "content": "latest"}]
    )

    result = await maybe_summarize(
        msgs, _SumConnector(), context_window=100, threshold=0.1
    )

    assert result[0]["role"] == "system"
    assert result[0]["content"] == "Original system."

    summary_indices = [
        i for i, m in enumerate(result)
        if m.get("role") == "system" and "[Summary" in m.get("content", "")
    ]
    assert summary_indices, "Summary message must exist"
    assert min(summary_indices) > 0, "Summary must come after original system header"


@pytest.mark.asyncio
async def test_summary_precedes_recent_messages_in_result():
    """The summary must appear before the recent verbatim messages."""

    class _SumConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            yield "compact summary"

    msgs = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": "old " * 100} for _ in range(6)]
        + [{"role": "user", "content": "RECENT_UNIQUE_MARKER"}]
    )

    result = await maybe_summarize(
        msgs, _SumConnector(), context_window=200, threshold=0.1
    )

    summary_idx = next(
        i for i, m in enumerate(result)
        if "[Summary" in m.get("content", "")
    )
    recent_idx = next(
        i for i, m in enumerate(result)
        if "RECENT_UNIQUE_MARKER" in m.get("content", "")
    )
    assert summary_idx < recent_idx, "Summary must precede recent messages"


# ---------------------------------------------------------------------------
# 5. Summarization failure preserves usable conversation history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_returns_original_messages():
    """If the LLM raises, the original message list is returned unchanged."""
    msgs = [
        {"role": "system", "content": "sys"},
        *[{"role": "user", "content": "x" * 400} for _ in range(6)],
        {"role": "user", "content": "latest"},
    ]

    result = await maybe_summarize(
        msgs, _FailingConnector(), context_window=100, threshold=0.1
    )

    assert result is msgs, "On failure, original messages must be returned unchanged"


@pytest.mark.asyncio
async def test_failure_preserves_all_history():
    """On failure, no messages are dropped — all history is preserved."""
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 300},
        {"role": "assistant", "content": "y" * 300},
        {"role": "user", "content": "latest turn"},
    ]

    result = await maybe_summarize(
        msgs, _FailingConnector(), context_window=50, threshold=0.1
    )

    assert len(result) == len(msgs)
    assert result[-1]["content"] == "latest turn"


@pytest.mark.asyncio
async def test_empty_summary_response_still_works(tmp_path):
    """An empty summary response still produces a valid (if empty) summary entry."""

    class _EmptyConnector:
        connector_type = "text"
        supports_tool_calling = False

        async def stream_chat_completion(self, messages, **kw) -> AsyncIterator[str]:
            # Yield nothing — empty summary
            if False:
                yield ""

    msgs = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": "x" * 300} for _ in range(6)]
        + [{"role": "user", "content": "recent"}]
    )

    result = await maybe_summarize(
        msgs, _EmptyConnector(), context_window=200, threshold=0.1
    )

    # Must not crash and must include the summary placeholder
    assert isinstance(result, list)
    summary_msgs = [m for m in result if "[Summary" in m.get("content", "")]
    assert len(summary_msgs) == 1


# ---------------------------------------------------------------------------
# 6. Web and Telegram share the same summarization system
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_service_calls_maybe_summarize(tmp_path):
    """ChatService.generate_reply passes messages through maybe_summarize."""
    from unittest.mock import patch

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

    from aubergeRP.services.chat_service import GenerationOptions

    captured: list = []

    original_maybe_summarize = __import__(
        "aubergeRP.services.summarization_service", fromlist=["maybe_summarize"]
    ).maybe_summarize

    async def _spy(messages, *args, **kwargs):
        captured.append(messages)
        return await original_maybe_summarize(messages, *args, **kwargs)

    with patch("aubergeRP.services.chat_service.maybe_summarize", side_effect=_spy):
        await svc.generate_reply(
            conversation_id=conv.id,
            content="Hello",
            options=GenerationOptions(),
        )

    assert len(captured) >= 1, "generate_reply must call maybe_summarize"


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
    """Both web and Telegram pass messages through maybe_summarize in ChatService."""
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
