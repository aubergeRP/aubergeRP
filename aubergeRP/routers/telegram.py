"""Telegram bot admin router."""
from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request

try:
    from aiogram import Bot  # noqa: F401 (imported here so tests can patch it)
except ImportError:
    Bot = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from ..services.telegram_runtime_manager import TelegramRuntimeManager

from ..services.telegram_bot_service import (
    TelegramBotCreate,
    TelegramBotInvalidError,
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
async def create_bot(
    data: TelegramBotCreate,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        result = svc.create_bot(data)
    except TelegramBotInvalidError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # A bot created as enabled must run immediately — otherwise its webhook is
    # never registered with Telegram and no update ever arrives.
    if result.enabled:
        await _start_bot(svc, result)
    return result


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
async def update_bot(
    bot_id: str,
    data: TelegramBotUpdate,
    svc: TelegramBotService = Depends(get_telegram_service),
    _token: str = Depends(get_admin_token),
) -> TelegramBotSummary:
    try:
        result = svc.update_bot(bot_id, data)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)
    except TelegramBotInvalidError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Apply the new settings immediately when the bot is already running
    # (update mode, webhook URL, character and token all change its runtime).
    mgr = _get_manager()
    if mgr is not None and mgr.is_running(bot_id):
        await mgr.restart_bot(bot_id)
    elif result.enabled:
        # The bot was enabled but not running (e.g. created before the runtime
        # was wired, or a previous start failed): start it now.
        await _start_bot(svc, result)
    return result


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
    await _start_bot(svc, result)
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

    result = svc.record_test_result(bot_id, tg_bot_id, tg_username, error)

    # Back-compat safety net: bots created before the runtime was started on
    # create/update may be enabled yet never launched — no webhook registered,
    # no profile sync.  A successful test connection starts them.
    if not error and result.enabled:
        if not _is_running(bot_id):
            await _start_bot(svc, result)
        else:
            # Already running: re-apply the profile so a setup that failed
            # halfway (missing description or profile photo) is repaired.
            mgr = _get_manager()
            if mgr is not None:
                await mgr.resync_bot_profile(bot_id, result.character_id)
    return result


# ── Webhook receiver ──────────────────────────────────────────────────────────


@router.post("/webhook/{bot_id}", status_code=200)
async def receive_webhook(
    bot_id: str,
    request: Request,
    background: BackgroundTasks,
    svc: TelegramBotService = Depends(get_telegram_service),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    """Receive updates pushed by Telegram in webhook mode.

    Called by Telegram itself, so it is not behind the admin token: the
    ``X-Telegram-Bot-Api-Secret-Token`` header is the authentication.
    """
    try:
        summary = svc.get_bot(bot_id)
    except TelegramBotNotFoundError:
        raise _not_found(bot_id)

    expected = svc.get_bot_webhook_secret(bot_id)
    if expected:
        provided = x_telegram_bot_api_secret_token or ""
        if not hmac.compare_digest(provided, expected):
            logger.warning("Telegram webhook for bot %s: bad secret token", bot_id)
            raise HTTPException(status_code=403, detail="Invalid secret token")

    if not summary.enabled:
        raise HTTPException(status_code=409, detail=f"Telegram bot '{bot_id}' is disabled")

    mgr = _get_manager()
    if mgr is None:
        raise HTTPException(status_code=503, detail="Telegram runtime is not available")

    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Invalid update payload")

    # Answer Telegram immediately; generating a reply can take a while and
    # Telegram would retry the update on timeout.
    background.add_task(mgr.dispatch_update, bot_id, update)
    return {"ok": True}


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


async def _start_bot(svc: TelegramBotService, summary: TelegramBotSummary) -> None:
    """Launch *summary* on the shared runtime manager (no-op if already running).

    Starting a bot is what registers its webhook with Telegram (webhook mode),
    starts long-polling (polling mode) and syncs its Telegram profile from the
    character card.
    """
    mgr = _get_manager()
    if mgr is None:
        return
    try:
        token = svc.get_bot_token(summary.id)
    except TelegramBotNotFoundError:
        return
    await mgr.start_bot(
        summary.id,
        token,
        summary.character_id,
        summary.dialogue_only,
        update_mode=summary.update_mode,
        webhook_url=summary.webhook_url,
    )


def _is_running(bot_id: str) -> bool:
    mgr = _get_manager()
    if mgr is None:
        return False
    return mgr.is_running(bot_id)
