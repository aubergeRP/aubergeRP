from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..event_bus import EventBus, get_event_bus
from ..models.chat import ChatMessageRequest
from ..services.character_service import CharacterService
from ..services.chat_service import ChatService
from ..services.conversation_service import ConversationService
from ..services.media_service import MediaService
from ..services.statistics_service import StatisticsService
from .connectors import get_connector_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

_KEEPALIVE_TIMEOUT = 30.0  # seconds between SSE keepalive comments


class RetryImageRequest(BaseModel):
    """Request to retry image generation with a stored prompt."""
    prompt: str
    generation_id: str


def get_session_token(x_session_token: str = Header(default="")) -> str:
    return x_session_token


def get_chat_service(
    session_token: str = Depends(get_session_token),
) -> ChatService:
    from ..config import get_config
    config = get_config()
    data_dir = config.app.data_dir
    char_svc = CharacterService(data_dir=data_dir)
    conv_svc = ConversationService(data_dir=data_dir, character_service=char_svc)
    stats_svc = StatisticsService(data_dir=data_dir)
    media_svc = MediaService(data_dir=data_dir)
    manager = get_connector_manager()
    images_dir = Path(data_dir) / "images" / (session_token or "anonymous")
    return ChatService(
        conversation_service=conv_svc,
        character_service=char_svc,
        connector_manager=manager,
        images_dir=images_dir,
        session_token=session_token or "anonymous",
        context_window=config.chat.context_window,
        summarization_threshold=config.chat.summarization_threshold,
        ooc_protection=config.chat.ooc_protection,
        statistics_service=stats_svc,
        media_service=media_svc,
        channel="web",
        channel_instance_id="web",
        external_user_id=session_token or "web-user",
        external_chat_id=session_token or "",
    )


@router.post("/{conversation_id}/message")
async def chat(
    conversation_id: str,
    body: ChatMessageRequest,
    session_token: str = Depends(get_session_token),
    service: ChatService = Depends(get_chat_service),
    bus: EventBus = Depends(get_event_bus),
) -> StreamingResponse:
    async def event_generator() -> AsyncGenerator[str, None]:
        async for event in service.stream_chat(conversation_id, body.content):
            await bus.publish(session_token, conversation_id, event)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/{conversation_id}/events")
async def chat_events(
    conversation_id: str,
    session_token: str = "",
    bus: EventBus = Depends(get_event_bus),
) -> StreamingResponse:
    """Long-lived SSE endpoint for multi-browser event delivery.

    Other browser tabs sharing the same session token subscribe here and
    receive every event published during chat, without having to be the tab
    that sent the message.  The connection is kept open with periodic
    keepalive comments so that EventSource auto-reconnect is not triggered.

    The session token is passed as the ``session_token`` query parameter
    (instead of a header) because the browser ``EventSource`` API does not
    support custom request headers.
    """
    q = bus.subscribe(session_token, conversation_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_TIMEOUT)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(session_token, conversation_id, q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{conversation_id}/generate-image")
async def generate_scene_image(
    conversation_id: str,
    session_token: str = Depends(get_session_token),
    service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    """Generate an image of the current scene from the conversation context.

    This endpoint triggers image generation using the active image connector,
    with a prompt built automatically from the recent conversation history via
    the active text connector.  It is called when the user clicks the
    "Generate scene image" button in the frontend.
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        async for event in service.generate_scene_image(conversation_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{conversation_id}/retry-image")
async def retry_image(
    conversation_id: str,
    body: RetryImageRequest,
    session_token: str = Depends(get_session_token),
    service: ChatService = Depends(get_chat_service),
) -> StreamingResponse:
    """Retry generation of a single image with the given prompt and generation_id.

    This endpoint is used when an image generation fails and the user clicks
    the "Retry" button. It generates just the image without sending the entire
    message through the chat flow again.
    """
    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            async for event in service.retry_generate_image(
                conversation_id=conversation_id,
                prompt=body.prompt,
                generation_id=body.generation_id,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(
                "[Retry Image] Error generating image (gen_id=%s)", body.generation_id
            )
            yield f"data: {json.dumps({
                'type': 'image_failed',
                'generation_id': body.generation_id,
                'detail': 'Image generation failed. Check server logs for details.',
            },
            ensure_ascii=False
            )}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
