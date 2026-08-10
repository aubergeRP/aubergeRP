"""Telegram bot admin router."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

try:
    from aiogram import Bot  # noqa: F401 (imported here so tests can patch it)
except ImportError:
    Bot = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from ..services.telegram_runtime_manager import TelegramRuntimeManager

from ..services.telegram_bot_service import (
    TelegramBotCreate,
    TelegramBotNotFoundError,
    TelegramBotService,
    TelegramBotSummary,
    TelegramBotUpdate,
)
from .admin import get_admin_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def get_telegram_service() -> TelegramBotService:
    from ..config import get_config
    return TelegramBotService(data_dir=get_config().app.data_dir)


def _not_found(bot_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Telegram bot '{bot_id}' not found")


# ── CRUD ─────────────────────────────────────────────────────────────────────


@router.get("/bots/", response_model=list[TelegramBotSummary])
def list_bots(
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> list[TelegramBotSummary]:
    return svc.list_bots()


@router.post("/bots/", status_code=201, response_model=TelegramBotSummary)
def create_bot(
    data: TelegramBotCreate,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    return svc.create_bot(data)


@router.get("/bots/{bot_id}", response_model=TelegramBotSummary)
def get_bot(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        return svc.get_bot(bot_id)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)


@router.patch("/bots/{bot_id}", response_model=TelegramBotSummary)
def update_bot(
    bot_id: str,
    data: TelegramBotUpdate,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        return svc.update_bot(bot_id, data)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)


@router.delete("/bots/{bot_id}", status_code=204)
async def delete_bot(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> None:
    try:
        svc.delete_bot(bot_id)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)
    # Also stop the bot if it is running.
    mgr = _get_manager()
    if mgr is not None:
        await mgr.stop_bot(bot_id)


@router.post("/bots/{bot_id}/enable", response_model=TelegramBotSummary)
async def enable_bot(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        result = svc.set_enabled(bot_id, True)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)
    mgr = _get_manager()
    if mgr is not None:
        token = svc.get_bot_token(bot_id)
        await mgr.start_bot(bot_id, token, result.character_id)
    return result


@router.post("/bots/{bot_id}/disable", response_model=TelegramBotSummary)
async def disable_bot(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        result = svc.set_enabled(bot_id, False)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)
    mgr = _get_manager()
    if mgr is not None:
        await mgr.stop_bot(bot_id)
    return result


# ── Test connection ───────────────────────────────────────────────────────────


@router.post("/bots/{bot_id}/test", response_model=TelegramBotSummary)
async def test_bot(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        svc.get_bot(bot_id)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)

    token = svc.get_bot_token(bot_id)
    tg_bot_id = ""
    tg_username = ""
    error = ""

    try:
        bot_cls = Bot
        if bot_cls is None:
            raise ImportError("aiogram not available")
        bot = bot_cls(token=token)
        try:
            me = await bot.get_me()
            tg_bot_id = str(me.id)
            tg_username = me.username or ""
        finally:
            await bot.session.close()
    except Exception as exc:
        error = str(exc).replace(token, "<token>")
        logger.warning("Telegram test connection failed for bot %s: %s", bot_id, error)

    return svc.record_test_result(bot_id, tg_bot_id, tg_username, error)


# ── Runtime status ────────────────────────────────────────────────────────────


@router.get("/bots/{bot_id}/status")
def bot_status(
    bot_id: str,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> dict[str, object]:
    try:
        summary = svc.get_bot(bot_id)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)
    running = _is_running(bot_id)
    return {
        "bot_id": bot_id,
        "enabled": summary.enabled,
        "running": running,
        "telegram_username": summary.telegram_username,
        "last_error": summary.last_error,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_manager() -> TelegramRuntimeManager | None:
    """Return the shared runtime manager if one exists."""
    try:
        from ..main import _telegram_manager  # type: ignore[attr-defined]
        mgr: TelegramRuntimeManager | None = _telegram_manager
        return mgr
    except (ImportError, AttributeError):
        return None


def _is_running(bot_id: str) -> bool:
    mgr = _get_manager()
    if mgr is None:
        return False
    return mgr.is_running(bot_id)
