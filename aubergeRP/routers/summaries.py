"""Conversation summary inspection API (admin).

GET    /summaries/                       — one row per conversation: context size,
                                           budget, and whether a summary exists
GET    /summaries/{conversation_id}      — the summary chain and the messages that
                                           follow the most recent summary
POST   /summaries/{conversation_id}/summarize — force a summary right now
DELETE /summaries/{conversation_id}      — drop the last summary (?all=true: all)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..services.character_service import CharacterService
from ..services.conversation_service import ConversationNotFoundError, ConversationService
from ..services.summarization_service import count_prompt_tokens, prompt_budget
from ..services.summary_service import SummaryService
from .admin import get_admin_token

router = APIRouter(prefix="/summaries", tags=["summaries"])


def _services() -> tuple[Any, ConversationService, SummaryService]:
    from ..config import get_config

    config = get_config()
    char_svc = CharacterService(data_dir=config.app.data_dir)
    conv_svc = ConversationService(data_dir=config.app.data_dir, character_service=char_svc)
    return config, conv_svc, SummaryService(config.app.data_dir)


def _summary_connector(config: Any) -> Any:
    from ..connectors.manager import ConnectorManager

    manager = ConnectorManager(data_dir=config.app.data_dir, config=config)
    return manager.get_text_connector("text_summarization") or manager.get_active_text_connector()


def _current_prompt(conv_svc: ConversationService, summary_svc: SummaryService,
                    conversation_id: str, config: Any) -> tuple[list[dict[str, str]], str, list[Any]]:
    """Return the prompt that would be sent right now, without summarizing."""
    from ..services.chat_service import build_prompt

    conv = conv_svc.get_conversation(conversation_id)
    char = CharacterService(data_dir=config.app.data_dir).get_character(conv.character_id)
    summary_text, history = summary_svc.split_history(
        conv, summary_svc.get_latest(conversation_id)
    )
    messages = build_prompt(
        conv, char, user_name=config.user.name,
        history=history, summary_text=summary_text or None,
    )
    return messages, summary_text, history


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "content": row.content,
        "covers_until_message_id": row.covers_until_message_id,
        "covers_message_count": row.covers_message_count,
        "based_on_summary_id": row.based_on_summary_id,
        "tokens": row.tokens,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/")
def list_summaries(token: str = Depends(get_admin_token)) -> list[dict[str, Any]]:
    config, conv_svc, summary_svc = _services()
    budget = prompt_budget(config.chat.context_window, config.chat.summarization_threshold)
    result: list[dict[str, Any]] = []
    for conv_summary in conv_svc.list_conversations():
        try:
            messages, _, history = _current_prompt(
                conv_svc, summary_svc, conv_summary.id, config
            )
        except (ConversationNotFoundError, KeyError):
            continue
        latest = summary_svc.get_latest(conv_summary.id)
        result.append({
            "conversation_id": conv_summary.id,
            "title": conv_summary.title,
            "character_name": conv_summary.character_name,
            "message_count": conv_summary.message_count,
            "messages_since_summary": len(history),
            "context_tokens": count_prompt_tokens(messages),
            "context_window": config.chat.context_window,
            "threshold": config.chat.summarization_threshold,
            "budget_tokens": budget,
            "summary_count": len(summary_svc.list_chain(conv_summary.id)),
            "last_summary_at": latest.created_at.isoformat() if latest else None,
            "updated_at": conv_summary.updated_at.isoformat(),
        })
    result.sort(key=lambda r: r["context_tokens"], reverse=True)
    return result


@router.get("/{conversation_id}")
def get_summary_detail(
    conversation_id: str, token: str = Depends(get_admin_token)
) -> dict[str, Any]:
    config, conv_svc, summary_svc = _services()
    try:
        messages, summary_text, history = _current_prompt(
            conv_svc, summary_svc, conversation_id, config
        )
    except (ConversationNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    chain = summary_svc.list_chain(conversation_id)
    return {
        "conversation_id": conversation_id,
        "context_tokens": count_prompt_tokens(messages),
        "budget_tokens": prompt_budget(
            config.chat.context_window, config.chat.summarization_threshold
        ),
        "context_window": config.chat.context_window,
        "threshold": config.chat.summarization_threshold,
        "summary": summary_text,
        "summaries": [_row_to_dict(row) for row in chain],
        "messages_since": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in history
        ],
    }


@router.post("/{conversation_id}/summarize")
async def force_summarize(
    conversation_id: str, token: str = Depends(get_admin_token)
) -> dict[str, Any]:
    from ..services.statistics_service import StatisticsService

    config, conv_svc, summary_svc = _services()
    try:
        conv = conv_svc.get_conversation(conversation_id)
    except (ConversationNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    connector = _summary_connector(config)
    if connector is None:
        raise HTTPException(status_code=400, detail="No active text connector")
    row = await summary_svc.summarize_now(
        conv, connector,
        statistics_service=StatisticsService(data_dir=config.app.data_dir),
    )
    if row is None:
        return {"created": False}
    return {"created": True, "summary": _row_to_dict(row)}


@router.delete("/{conversation_id}")
def delete_summaries(
    conversation_id: str,
    whole_chain: bool = Query(
        default=False, alias="all", description="Delete the whole summary chain"
    ),
    token: str = Depends(get_admin_token),
) -> dict[str, Any]:
    _, _conv_svc, summary_svc = _services()
    if whole_chain:
        return {"deleted": summary_svc.delete_all(conversation_id)}
    return {"deleted": 1 if summary_svc.delete_latest(conversation_id) else 0}
