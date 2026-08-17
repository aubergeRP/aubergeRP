"""TelegramRuntimeManager — runs multiple aiogram bots concurrently.

Design
------
- Each enabled bot runs in its own asyncio Task.
- Polling mode: aiogram's built-in long-polling loop.
- Webhook mode: sets a Telegram webhook then keeps a sentinel task alive;
  incoming updates are dispatched via ``dispatch_update`` called by the
  HTTP router.
- Bots are isolated: stopping one does not affect others.
- Generation is serialized *per-conversation* using per-key asyncio.Lock stored
  in a dict.  Different users on the same bot, and same user across different
  bots, all run independently.
- Tokens are never logged.

Private-chat-only MVP
---------------------
Group and supergroup messages are silently ignored.

Media handling
--------------
- Incoming photos: the largest available photo is downloaded and attached as
  ``image_bytes`` to the generation call.  Caption is used as text content.
  If no caption is provided a placeholder is used.
- Outgoing images: when the generation result includes image paths the images
  are sent via ``send_photo`` before the text reply.

Bot profile sync
----------------
When a bot starts — and again on the admin "test connection" action, which acts
as a repair path — its Telegram profile (name, description, short description
and profile photo) is synchronised from the bound character card.  Text fields
are only pushed when they differ from what Telegram already holds, and are read
back once to make sure the change stuck.  The descriptions are LLM-rewritten to
fit Telegram's length limits (cached per card revision); when the character has
no avatar at all, one is generated from the card as a profile picture.  The
photo is only re-uploaded when the avatar changed (tracked by a hash marker
file).  Failures are logged and never prevent the bot from running.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from ..services.observability_service import get_registry, record_error, register_secret

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher
    from aiogram.types import Message

    from ..services.chat_service import GenerationResult

logger = logging.getLogger(__name__)

# Maximum Telegram message length (Bot API limit).
_TG_MAX_LEN = 4096

# Telegram clears a chat action after ~5s — refresh slightly before that.
_TG_CHAT_ACTION_REFRESH_S = 4.0

# Bot API limits for the bot profile fields.
_TG_MAX_NAME_LEN = 64
_TG_MAX_DESCRIPTION_LEN = 512
_TG_MAX_SHORT_DESCRIPTION_LEN = 120

# Sent when generation failed after ChatService exhausted its retries.  Kept
# terse and in-character: an error bubble would break immersion, but staying
# fully silent leaves the user waiting for a reply that never comes.
GENERATION_FAILURE_MESSAGE = "Sorry, say again?"


def _truncate(text: str, max_len: int) -> str:
    """Trim *text* to *max_len* characters, preferring a word boundary."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1]  # leave room for the ellipsis
    idx = cut.rfind(" ")
    if idx >= max_len // 2:
        cut = cut[:idx]
    return cut.rstrip(" ,;:-") + "…"


def _parse_json_object(raw: str) -> dict[str, object]:
    """Extract the first JSON object from a model answer (code fences tolerated)."""
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return {}
    parsed = json.loads(raw[start : end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _png_to_jpeg(raw: bytes) -> bytes:
    """Convert image bytes to a JPEG — Telegram only accepts .JPG profile photos."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(raw)) as img:
        rgb = img.convert("RGB")
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=90)
    return out.getvalue()


@contextlib.asynccontextmanager
async def chat_action(bot: Bot, chat_id: str, action: str = "typing") -> AsyncIterator[None]:
    """Show a Telegram status ("typing"/"upload_photo") for the duration of the block.

    Telegram clears the status after ~5s, so it is re-sent periodically.
    Failures are ignored: the status is cosmetic and must never break delivery.
    """
    async def _loop() -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id=int(chat_id), action=action)
            except Exception:
                return
            await asyncio.sleep(_TG_CHAT_ACTION_REFRESH_S)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def split_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
    """Split a long text into chunks ≤ max_len, preferring paragraph breaks.

    Blank chunks are dropped: Telegram rejects empty message text, so an empty
    reply must yield no chunks at all rather than one unsendable chunk.
    """
    if not text.strip():
        return []
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        # Try to split at a double-newline (paragraph boundary).
        idx = remaining.rfind("\n\n", 0, max_len)
        if idx == -1:
            # Fall back to any newline.
            idx = remaining.rfind("\n", 0, max_len)
        if idx == -1:
            # Hard split.
            idx = max_len
        parts.append(remaining[:idx])
        remaining = remaining[idx:]
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p.strip()]


class TelegramRuntimeManager:
    """Manages the lifecycle of one aiogram Dispatcher per Telegram bot."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        # bot_id → asyncio.Task
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # per-conversation asyncio.Lock for serialized generation
        self._conv_locks: dict[str, asyncio.Lock] = {}
        # bot_id → Dispatcher (for webhook dispatch)
        self._dispatchers: dict[str, Dispatcher] = {}
        # bot_id → Bot instance (for webhook dispatch)
        self._bots: dict[str, Bot] = {}
        # bot_id → current update_mode ("polling" | "webhook")
        self._modes: dict[str, str] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    async def start_enabled_bots(self) -> None:
        from .telegram_bot_service import TelegramBotService
        svc = TelegramBotService(self._data_dir)
        for summary, token in svc.list_enabled_bots_with_tokens():
            await self.start_bot(
                summary.id,
                token,
                summary.character_id,
                summary.dialogue_only,
                update_mode=summary.update_mode,
                webhook_url=summary.webhook_url,
            )

    async def start_bot(
        self,
        bot_id: str,
        token: str,
        character_id: str,
        dialogue_only: bool = False,
        *,
        update_mode: str = "polling",
        webhook_url: str = "",
    ) -> None:
        if bot_id in self._tasks and not self._tasks[bot_id].done():
            return
        self._modes[bot_id] = update_mode
        register_secret(token)
        get_registry().mark_bot_started(bot_id, update_mode)
        if update_mode == "webhook":
            task = asyncio.create_task(
                self._run_bot_webhook(bot_id, token, character_id, dialogue_only, webhook_url),
                name=f"telegram-bot-wh-{bot_id}",
            )
        else:
            task = asyncio.create_task(
                self._run_bot(bot_id, token, character_id, dialogue_only),
                name=f"telegram-bot-{bot_id}",
            )
        self._tasks[bot_id] = task

    async def stop_bot(self, bot_id: str) -> None:
        # If running in webhook mode, deregister the webhook with Telegram.
        if self._modes.get(bot_id) == "webhook" and bot_id in self._bots:
            with contextlib.suppress(Exception):
                await self._bots[bot_id].delete_webhook(drop_pending_updates=False)
        task = self._tasks.pop(bot_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._dispatchers.pop(bot_id, None)
        bot = self._bots.pop(bot_id, None)
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        self._modes.pop(bot_id, None)
        get_registry().mark_bot_stopped(bot_id)

    async def restart_bot(self, bot_id: str) -> None:
        from .telegram_bot_service import TelegramBotService
        await self.stop_bot(bot_id)
        svc = TelegramBotService(self._data_dir)
        try:
            summary = svc.get_bot(bot_id)
        except KeyError:
            return
        if summary.enabled:
            token = svc.get_bot_token(bot_id)
            await self.start_bot(
                bot_id,
                token,
                summary.character_id,
                summary.dialogue_only,
                update_mode=summary.update_mode,
                webhook_url=summary.webhook_url,
            )

    async def stop_all(self) -> None:
        for bot_id in list(self._tasks):
            await self.stop_bot(bot_id)

    def is_running(self, bot_id: str) -> bool:
        task = self._tasks.get(bot_id)
        return task is not None and not task.done()

    async def dispatch_update(self, bot_id: str, update_data: dict[str, object]) -> None:
        """Feed a raw Telegram update dict into the dispatcher for *bot_id*.

        Used by the webhook HTTP endpoint to process incoming updates without
        polling.
        """
        dp = self._dispatchers.get(bot_id)
        bot = self._bots.get(bot_id)
        if dp is None or bot is None:
            logger.warning("dispatch_update: bot %s has no active dispatcher", bot_id)
            return
        try:
            from aiogram.types import Update
            update = Update.model_validate(update_data)
            await dp.feed_update(bot, update)
        except Exception as exc:
            logger.exception("dispatch_update: failed to process update for bot %s", bot_id)
            get_registry().mark_bot_error(bot_id, str(exc))
            record_error("telegram_webhook", f"update dispatch failed: {exc}", bot_id=bot_id)

    # ── Internal: run one bot (polling) ──────────────────────────────────────

    async def _run_bot(self, bot_id: str, token: str, character_id: str, dialogue_only: bool = False) -> None:
        try:
            from aiogram import Bot, Dispatcher
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
            )
            dp = Dispatcher()
            self._bots[bot_id] = bot
            self._dispatchers[bot_id] = dp

            self._register_handlers(dp, bot_id, character_id, bot, dialogue_only)
            await self._sync_bot_profile(bot_id, bot, character_id)

            # Ensure no leftover webhook is registered before starting polling.
            with contextlib.suppress(Exception):
                await bot.delete_webhook(drop_pending_updates=False)

            logger.info("Telegram bot %s: starting polling", bot_id)
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            logger.info("Telegram bot %s: polling stopped", bot_id)
        except Exception as exc:
            logger.exception("Telegram bot %s: unexpected error, polling stopped", bot_id)
            get_registry().mark_bot_error(bot_id, str(exc))
            record_error("telegram_polling", f"polling stopped: {exc}", bot_id=bot_id)
        finally:
            self._dispatchers.pop(bot_id, None)
            b = self._bots.pop(bot_id, None)
            if b is not None:
                with contextlib.suppress(Exception):
                    await b.session.close()

    # ── Internal: run one bot (webhook sentinel) ──────────────────────────────

    async def _run_bot_webhook(
        self,
        bot_id: str,
        token: str,
        character_id: str,
        dialogue_only: bool,
        webhook_url: str,
    ) -> None:
        try:
            from aiogram import Bot, Dispatcher
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            from .telegram_bot_service import TelegramBotService

            bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
            )
            dp = Dispatcher()
            self._bots[bot_id] = bot
            self._dispatchers[bot_id] = dp

            self._register_handlers(dp, bot_id, character_id, bot, dialogue_only)
            await self._sync_bot_profile(bot_id, bot, character_id)

            # Retrieve current webhook secret from the DB.
            svc = TelegramBotService(self._data_dir)
            try:
                webhook_secret = svc.get_bot_webhook_secret(bot_id)
                register_secret(webhook_secret)
            except KeyError:
                webhook_secret = ""

            full_url = f"{webhook_url.rstrip('/')}/api/telegram/webhook/{bot_id}"
            logger.info("Telegram bot %s: setting webhook to %s", bot_id, full_url)
            try:
                await bot.set_webhook(
                    url=full_url,
                    secret_token=webhook_secret or None,
                    drop_pending_updates=False,
                )
                svc.record_webhook_error(bot_id, "")
            except Exception as exc:
                err = str(exc)
                logger.warning("Telegram bot %s: set_webhook failed: %s", bot_id, err)
                svc.record_webhook_error(bot_id, err)
                get_registry().mark_bot_error(bot_id, err)
                record_error("telegram_webhook", f"set_webhook failed: {err}", bot_id=bot_id)

            # Keep the task alive until cancelled — updates arrive via the HTTP
            # endpoint and are dispatched through dispatch_update().
            logger.info("Telegram bot %s: webhook mode active, waiting for updates", bot_id)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                logger.info("Telegram bot %s: webhook sentinel stopped", bot_id)
                raise
        except asyncio.CancelledError:
            logger.info("Telegram bot %s: webhook sentinel cancelled", bot_id)
            raise
        except Exception as exc:
            logger.exception("Telegram bot %s: unexpected error in webhook sentinel", bot_id)
            get_registry().mark_bot_error(bot_id, str(exc))
            record_error("telegram_webhook", f"webhook sentinel stopped: {exc}", bot_id=bot_id)
        finally:
            self._dispatchers.pop(bot_id, None)
            b = self._bots.pop(bot_id, None)
            if b is not None:
                with contextlib.suppress(Exception):
                    await b.session.close()

    # ── Bot profile sync ─────────────────────────────────────────────────────

    async def resync_bot_profile(self, bot_id: str, character_id: str) -> None:
        """Re-run the profile sync for an already running bot (repair path).

        Used by the admin "test connection" action so a partially applied setup
        (missing description or profile photo) can be fixed without restarting.
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return
        await self._sync_bot_profile(bot_id, bot, character_id)

    async def _sync_bot_profile(self, bot_id: str, bot: Bot, character_id: str) -> None:
        """Push the character's name / description / avatar to the bot profile.

        Best effort: any failure is logged and swallowed so a bot always starts.
        """
        try:
            from .character_service import CharacterService
            char_svc = CharacterService(data_dir=self._data_dir)
            char = char_svc.get_character(character_id)
        except Exception:
            logger.warning("Telegram bot %s: cannot load character for profile sync", bot_id)
            return

        name = char.data.name.strip()[:_TG_MAX_NAME_LEN]
        description, short_description = await self._profile_texts(character_id, char)

        await self._set_if_changed(bot_id, "name", bot.get_my_name, bot.set_my_name, name)
        await self._set_if_changed(
            bot_id, "description", bot.get_my_description, bot.set_my_description, description
        )
        await self._set_if_changed(
            bot_id,
            "short_description",
            bot.get_my_short_description,
            bot.set_my_short_description,
            short_description,
        )

        avatar_path = None
        with contextlib.suppress(Exception):
            avatar_path = await self._ensure_avatar(bot_id, character_id, char)
        if avatar_path is not None:
            await self._sync_profile_photo(bot_id, bot, avatar_path)

    async def _set_if_changed(
        self,
        bot_id: str,
        field: str,
        getter: object,
        setter: object,
        value: str,
    ) -> None:
        """Ensure Telegram holds *value* for *field*, retrying once on mismatch.

        The read-back retry repairs a setup where a previous call silently failed
        (flood wait, transient error) and left the field empty or stale.
        """
        if not value:
            return
        applied = False
        for _ in range(2):
            try:
                current = await getter()  # type: ignore[operator]
                if getattr(current, field, None) == value:
                    if applied:
                        logger.info("Telegram bot %s: profile %s updated", bot_id, field)
                    return
                await setter(value)  # type: ignore[operator]
                applied = True
            except Exception as exc:
                logger.warning("Telegram bot %s: failed to sync %s: %s", bot_id, field, exc)
                return
        logger.warning("Telegram bot %s: profile %s still not applied", bot_id, field)

    async def _profile_texts(self, character_id: str, char: object) -> tuple[str, str]:
        """Return (description, short_description) fitting Telegram's limits.

        The card description is usually far too long (and written for the LLM,
        not for humans), so an LLM rewrite is generated once per card revision
        and cached on disk.  Any failure falls back to a truncated card text.
        """
        data = char.data  # type: ignore[attr-defined]
        raw_description = data.description.strip()
        raw_notes = data.creator_notes.strip()
        fallback = (
            _truncate(raw_description, _TG_MAX_DESCRIPTION_LEN),
            _truncate(raw_notes or raw_description, _TG_MAX_SHORT_DESCRIPTION_LEN),
        )
        if not raw_description and not raw_notes:
            return fallback

        digest = hashlib.sha256(
            f"{data.name}\x00{raw_description}\x00{raw_notes}".encode()
        ).hexdigest()
        cache = self._data_dir / "telegram_profile" / f"{character_id}.bio.json"
        with contextlib.suppress(Exception):
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("digest") == digest:
                return cached["description"], cached["short_description"]

        try:
            from ..config import get_config
            from ..connectors.manager import ConnectorManager
            from .prompt_service import get_prompt

            connector = ConnectorManager(
                data_dir=self._data_dir, config=get_config()
            ).get_text_connector("text_utility")
            if connector is None:
                return fallback
            user_content = get_prompt("telegram_profile_bio").format(
                char_name=data.name,
                char_description=raw_description or "(none)",
                char_creator_notes=raw_notes or "(none)",
                max_description=_TG_MAX_DESCRIPTION_LEN,
                max_short_description=_TG_MAX_SHORT_DESCRIPTION_LEN,
            )
            tokens: list[str] = []
            async for chunk in connector.stream_chat_completion(
                [{"role": "user", "content": user_content}],
                temperature=0.7,
            ):
                tokens.append(chunk)
            payload = _parse_json_object("".join(tokens))
            description = _truncate(
                str(payload.get("description", "")).strip(), _TG_MAX_DESCRIPTION_LEN
            )
            short_description = _truncate(
                str(payload.get("short_description", "")).strip().replace("\n", " "),
                _TG_MAX_SHORT_DESCRIPTION_LEN,
            )
        except Exception as exc:
            logger.warning("Telegram: profile bio generation failed for %s: %s", character_id, exc)
            return fallback

        if not description or not short_description:
            return fallback

        with contextlib.suppress(Exception):
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({
                    "digest": digest,
                    "description": description,
                    "short_description": short_description,
                }),
                encoding="utf-8",
            )
            os.rename(tmp, cache)
        return description, short_description

    async def _ensure_avatar(self, bot_id: str, character_id: str, char: object) -> Path | None:
        """Return the character avatar, generating one when it is missing.

        A bot without a profile photo cannot be repaired from the admin panel
        when the character card has no avatar, so one is generated from the
        card description, framed as a profile picture.
        """
        from .character_service import CharacterService

        char_svc = CharacterService(data_dir=self._data_dir)
        existing = char_svc.get_avatar_path(character_id)
        if existing is not None:
            return existing

        data = char.data  # type: ignore[attr-defined]
        try:
            from ..config import get_config
            from ..connectors.manager import ConnectorManager
            from .prompt_service import get_prompt

            manager = ConnectorManager(data_dir=self._data_dir, config=get_config())
            img_connector = manager.get_active_image_connector()
            if img_connector is None:
                return None

            prompt = ""
            text_connector = manager.get_text_connector("text_utility")
            if text_connector is not None:
                template = get_prompt("telegram_profile_image").format(
                    char_name=data.name,
                    char_description=(data.description or "")[:600],
                )
                tokens: list[str] = []
                async for chunk in text_connector.stream_chat_completion(
                    [{"role": "user", "content": template}],
                    temperature=0.7,
                ):
                    tokens.append(chunk)
                prompt = "".join(tokens).strip()
            if not prompt:
                desc = (data.description or "")[:300]
                prompt = (
                    f"Head and shoulders portrait of {data.name}. {desc} "
                    "Centered profile picture, soft diffused light, sharp focus."
                ).strip()

            auberge = data.extensions.get("aubergerp", {})
            prefix = auberge.get("image_prompt_prefix", "")
            negative = auberge.get("negative_prompt", "")
            full_prompt = f"{prefix} {prompt}".strip() if prefix else prompt

            img_bytes: bytes | None = None
            async for event in img_connector.generate_image_with_progress(
                full_prompt, negative_prompt=negative
            ):
                if event["type"] == "complete":
                    img_bytes = event["bytes"]
            if not img_bytes:
                logger.warning("Telegram bot %s: avatar generation returned no image", bot_id)
                return None
            char_svc.save_avatar(character_id, img_bytes)
        except Exception as exc:
            logger.warning("Telegram bot %s: avatar generation failed: %s", bot_id, exc)
            return None

        logger.info("Telegram bot %s: generated a profile avatar for %s", bot_id, character_id)
        return char_svc.get_avatar_path(character_id)

    async def _sync_profile_photo(self, bot_id: str, bot: Bot, avatar_path: Path) -> None:
        """Upload the character avatar as the bot's profile photo (once per change)."""
        marker = self._data_dir / "telegram_profile" / f"{bot_id}.sha256"
        try:
            raw = avatar_path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if marker.exists() and marker.read_text().strip() == digest:
                return

            jpeg = _png_to_jpeg(raw)

            from aiogram.types import BufferedInputFile, InputProfilePhotoStatic
            await bot.set_my_profile_photo(
                photo=InputProfilePhotoStatic(
                    photo=BufferedInputFile(jpeg, filename="avatar.jpg"),
                )
            )
        except Exception as exc:
            logger.warning("Telegram bot %s: failed to sync profile photo: %s", bot_id, exc)
            return

        with contextlib.suppress(Exception):
            marker.parent.mkdir(parents=True, exist_ok=True)
            tmp = marker.with_suffix(".tmp")
            tmp.write_text(digest)
            os.rename(tmp, marker)
        logger.info("Telegram bot %s: profile photo updated", bot_id)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _register_handlers(
        self,
        dp: Dispatcher,
        bot_id: str,
        character_id: str,
        bot: Bot,
        dialogue_only: bool = False,
    ) -> None:
        from aiogram import F
        from aiogram.filters import Command
        from aiogram.types import Message  # noqa: F401 (used in annotations)

        @dp.message(Command("start"))
        async def on_start(message: Message) -> None:
            if not self._is_private(message):
                return
            user_id = str(message.from_user.id) if message.from_user else "0"
            chat_id = str(message.chat.id)
            conv_id, created = await asyncio.get_running_loop().run_in_executor(
                None, self._get_or_create_session, bot_id, user_id, chat_id, character_id
            )
            if created:
                await message.answer("👋 Session started! Say something to begin.")
            else:
                await message.answer("👋 Welcome back! Your session is already active.")

        @dp.message(Command("reset"))
        async def on_reset(message: Message) -> None:
            if not self._is_private(message):
                return
            user_id = str(message.from_user.id) if message.from_user else "0"
            chat_id = str(message.chat.id)
            _conv_id, greeting = await asyncio.get_running_loop().run_in_executor(
                None, self._reset_session, bot_id, user_id, chat_id, character_id
            )
            await message.answer("🔄 Conversation reset. Starting fresh!")
            for chunk in (split_message(greeting) if greeting.strip() else []):
                try:
                    await message.answer(chunk)
                except Exception as exc:
                    get_registry().mark_delivery_failure(bot_id)
                    record_error("telegram_delivery", f"greeting send failed: {exc}",
                                 bot_id=bot_id, conversation_id=_conv_id)
                    break
                else:
                    get_registry().mark_message_sent(bot_id)

        @dp.message(Command("status"))
        async def on_status(message: Message) -> None:
            if not self._is_private(message):
                return
            # Safe info only — no token, no internal IDs
            from .character_service import CharacterService
            from .telegram_bot_service import TelegramBotService
            from .timezone_service import TimezoneService
            svc = TelegramBotService(self._data_dir)
            try:
                summary = svc.get_bot(bot_id)
            except KeyError:
                await message.answer("❌ Bot configuration not found.")
                return
            char_name = character_id
            try:
                char_svc = CharacterService(self._data_dir)
                char = char_svc.get_character(character_id)
                char_name = char.data.name
            except Exception:
                pass
            user_id = str(message.from_user.id) if message.from_user else "0"
            tz_svc = TimezoneService(self._data_dir)
            tz_name = tz_svc.get_timezone_name("telegram", bot_id, user_id)
            tz_line = f"🌍 Timezone: {tz_name}" if tz_name else "🌍 Timezone: not set (use /timezone Europe/Paris)"
            status_text = (
                f"🤖 Bot: {summary.name}\n"
                f"👤 Character: {char_name}\n"
                f"🟢 Status: {'Running' if self.is_running(bot_id) else 'Stopped'}\n"
                f"{tz_line}"
            )
            await message.answer(status_text)

        @dp.message(Command("timezone"))
        async def on_timezone(message: Message) -> None:
            if not self._is_private(message):
                return
            from .timezone_service import InvalidTimezoneError, TimezoneService
            user_id = str(message.from_user.id) if message.from_user else "0"
            text = message.text or ""
            # Extract argument after /timezone
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                tz_svc = TimezoneService(self._data_dir)
                tz_name = tz_svc.get_timezone_name("telegram", bot_id, user_id)
                if tz_name:
                    await message.answer(f"🌍 Your current timezone: {tz_name}\n\nTo change it: /timezone Europe/Paris")
                else:
                    await message.answer(
                        "🌍 No timezone configured.\n\nUse: /timezone <IANA name>\nExample: /timezone Europe/Paris"
                    )
                return
            tz_arg = parts[1].strip()
            tz_svc = TimezoneService(self._data_dir)
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, tz_svc.set_timezone, "telegram", bot_id, user_id, tz_arg
                )
                from .schedule_instance_service import ScheduleInstanceService
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: ScheduleInstanceService(self._data_dir).update_timezone_for_user(
                        channel="telegram",
                        channel_instance_id=bot_id,
                        external_user_id=user_id,
                        timezone=tz_arg,
                    ),
                )
            except InvalidTimezoneError as exc:
                await message.answer(f"❌ {exc}")
                return
            await message.answer(f"✅ Timezone set to: {tz_arg}")

        @dp.message(F.chat.type == "private")
        async def on_message(message: Message) -> None:
            user_id = str(message.from_user.id) if message.from_user else "0"
            chat_id = str(message.chat.id)

            get_registry().mark_update_received(bot_id)

            # Resolve text content (message text or photo caption).
            text = message.text or message.caption or ""

            # Download photo if present.
            image_bytes: bytes | None = None
            if message.photo:
                largest = max(message.photo, key=lambda p: p.file_size or 0)
                try:
                    buf = await bot.download(largest.file_id)
                    image_bytes = buf.read() if buf is not None else None
                except Exception:
                    logger.warning(
                        "Telegram bot %s: failed to download photo for user %s",
                        bot_id, user_id,
                    )

            # If no text and no image, skip silently.
            if not text.strip() and image_bytes is None:
                return

            # Provide a default message when only an image is sent.
            if not text.strip() and image_bytes is not None:
                text = "[image]"

            conv_id, _ = await asyncio.get_running_loop().run_in_executor(
                None, self._get_or_create_session, bot_id, user_id, chat_id, character_id
            )

            # Serialize generation per conversation
            lock = self._get_conv_lock(conv_id)
            async with lock, chat_action(bot, chat_id):
                try:
                    result = await self._generate(
                        conv_id,
                        text,
                        bot_id=bot_id,
                        user_id=user_id,
                        chat_id=chat_id,
                        dialogue_only=dialogue_only,
                    )
                except Exception as exc:
                    logger.exception("Telegram bot %s: generation failed for conv %s", bot_id, conv_id)
                    record_error("llm", f"telegram generation failed: {exc}",
                                 bot_id=bot_id, conversation_id=conv_id)
                    # ChatService already retried with backoff.  Acknowledge in
                    # character rather than showing an error bubble.
                    try:
                        await message.answer(GENERATION_FAILURE_MESSAGE)
                    except Exception as send_exc:
                        get_registry().mark_delivery_failure(bot_id)
                        record_error(
                            "telegram_delivery",
                            f"failure notice send failed: {send_exc}",
                            bot_id=bot_id, conversation_id=conv_id,
                        )
                    return

            # Send generated images first, then text reply.
            # result.images contains URL paths (/api/images/<session>/<file>).
            # Resolve each URL to its actual filesystem path via data_dir.
            for image_url in result.images:
                filename = Path(image_url).name
                image_path = Path(self._data_dir) / "images" / "telegram" / filename
                if image_path.exists():
                    try:
                        chat_id_int = int(chat_id)
                        with image_path.open("rb") as img_fh:
                            from aiogram.types import BufferedInputFile
                            img_data = BufferedInputFile(img_fh.read(), filename=image_path.name)
                            async with chat_action(bot, chat_id, "upload_photo"):
                                await bot.send_photo(chat_id=chat_id_int, photo=img_data)
                    except Exception as exc:
                        logger.error(
                            "Telegram bot %s: failed to send image %s to conv %s",
                            bot_id, image_url, conv_id,
                        )
                        get_registry().mark_delivery_failure(bot_id)
                        record_error("telegram_delivery", f"image send failed: {exc}",
                                     bot_id=bot_id, conversation_id=conv_id)
                else:
                    logger.warning(
                        "Telegram bot %s: generated image not found on disk: %s",
                        bot_id, image_path,
                    )

            # Deliver text reply (split if needed) — failure does NOT re-generate.
            chunks = split_message(result.text)
            for chunk in chunks:
                try:
                    await message.answer(chunk)
                except Exception as exc:
                    logger.error(
                        "Telegram bot %s: delivery failed for conv %s (chunk len=%d)",
                        bot_id, conv_id, len(chunk),
                    )
                    get_registry().mark_delivery_failure(bot_id)
                    record_error("telegram_delivery", f"text send failed: {exc}",
                                 bot_id=bot_id, conversation_id=conv_id)
                    break
                else:
                    get_registry().mark_message_sent(bot_id)

        # Ignore non-private chats silently (no handler registered for them)

    # ── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_private(message: Message) -> bool:
        return message.chat.type == "private"

    def _get_conv_lock(self, conv_id: str) -> asyncio.Lock:
        if conv_id not in self._conv_locks:
            self._conv_locks[conv_id] = asyncio.Lock()
        return self._conv_locks[conv_id]

    def _get_or_create_session(
        self,
        bot_id: str,
        user_id: str,
        chat_id: str,
        character_id: str,
    ) -> tuple[str, bool]:
        from .channel_session_service import ChannelSessionService
        svc = ChannelSessionService(self._data_dir)
        conv_id, created = svc.get_or_create(
            channel="telegram",
            channel_instance_id=bot_id,
            external_user_id=user_id,
            external_chat_id=chat_id,
            character_id=character_id,
        )
        self._ensure_schedule_instances(
            bot_id=bot_id,
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
            conversation_id=conv_id,
        )
        return conv_id, created

    def _ensure_schedule_instances(
        self,
        bot_id: str,
        user_id: str,
        chat_id: str,
        character_id: str,
        conversation_id: str,
    ) -> None:
        """Create schedule instances for all enabled schedule definitions in the character card."""
        try:
            from ..models.character import ProactiveConfig, ScheduleDefinition
            from .character_service import CharacterNotFoundError, CharacterService
            from .schedule_instance_service import ScheduleInstanceService
            from .timezone_service import TimezoneService

            char_svc = CharacterService(data_dir=self._data_dir)
            try:
                char = char_svc.get_character(character_id)
            except (CharacterNotFoundError, KeyError):
                return

            ext = char.data.extensions.get("aubergerp", {})
            schedules_raw = ext.get("schedules", [])
            if not schedules_raw:
                return
            proactive_cfg = ProactiveConfig(**(ext.get("proactive", {}) if isinstance(ext, dict) else {}))

            tz_svc = TimezoneService(self._data_dir)
            timezone = tz_svc.get_timezone_name("telegram", bot_id, user_id) or "UTC"

            sched_svc = ScheduleInstanceService(self._data_dir)
            for raw in schedules_raw:
                if not isinstance(raw, dict):
                    continue
                try:
                    defn = ScheduleDefinition(**raw)
                except Exception:
                    continue
                try:
                    sched_svc.get_or_create(
                        defn=defn,
                        character_id=character_id,
                        conversation_id=conversation_id,
                        channel="telegram",
                        channel_instance_id=bot_id,
                        external_user_id=user_id,
                        external_chat_id=chat_id,
                        timezone=timezone,
                        origin="character-card",
                        decision_mode=proactive_cfg.decision_mode,
                        proactive=proactive_cfg,
                    )
                except Exception:
                    logger.warning(
                        "Failed to ensure schedule instance for def '%s' conv '%s'",
                        defn.id,
                        conversation_id,
                        exc_info=True,
                    )
        except Exception:
            logger.warning("_ensure_schedule_instances failed", exc_info=True)

    def _reset_session(
        self,
        bot_id: str,
        user_id: str,
        chat_id: str,
        character_id: str,
    ) -> tuple[str, str]:
        """Start a fresh conversation.  Returns (conversation_id, greeting).

        The greeting is the character's ``first_mes`` (already stored as the
        first assistant message by ``create_conversation``), or "" when the
        card defines none.
        """
        from .channel_session_service import ChannelSessionService
        from .character_service import CharacterService
        from .conversation_service import ConversationService
        from .schedule_instance_service import ScheduleInstanceService
        svc = ChannelSessionService(self._data_dir)
        old_conv_id = svc.get_conversation_id("telegram", bot_id, user_id)
        conv_id = svc.reset(
            channel="telegram",
            channel_instance_id=bot_id,
            external_user_id=user_id,
            external_chat_id=chat_id,
            character_id=character_id,
        )
        # Schedule instances point at a conversation: drop the stale ones and
        # recreate them for the fresh conversation.
        if old_conv_id and old_conv_id != conv_id:
            with contextlib.suppress(Exception):
                ScheduleInstanceService(self._data_dir).delete_for_conversation(old_conv_id)
        self._ensure_schedule_instances(
            bot_id=bot_id,
            user_id=user_id,
            chat_id=chat_id,
            character_id=character_id,
            conversation_id=conv_id,
        )
        greeting = ""
        try:
            char_svc = CharacterService(data_dir=self._data_dir)
            conv_svc = ConversationService(data_dir=self._data_dir, character_service=char_svc)
            conv = conv_svc.get_conversation(conv_id)
            for msg in conv.messages:
                if msg.role == "assistant":
                    greeting = msg.content
                    break
        except Exception:
            logger.warning("Telegram bot %s: failed to load greeting for conv %s",
                           bot_id, conv_id, exc_info=True)
        return conv_id, greeting

    async def _generate(
        self,
        conv_id: str,
        text: str,
        *,
        bot_id: str = "",
        user_id: str = "0",
        chat_id: str = "",
        dialogue_only: bool = False,
    ) -> GenerationResult:
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.chat_service import ChatService, GenerationOptions
        from ..services.conversation_service import ConversationService
        from ..services.media_service import MediaService
        from ..services.statistics_service import StatisticsService

        config = get_config()
        data_dir = self._data_dir
        char_svc = CharacterService(data_dir=data_dir)
        conv_svc = ConversationService(data_dir=data_dir, character_service=char_svc)
        stats_svc = StatisticsService(data_dir=data_dir)
        media_svc = MediaService(data_dir=data_dir)
        connector_manager = ConnectorManager(data_dir=data_dir, config=config)
        images_dir = Path(data_dir) / "images" / "telegram"

        svc = ChatService(
            conversation_service=conv_svc,
            character_service=char_svc,
            connector_manager=connector_manager,
            images_dir=images_dir,
            session_token="telegram",
            context_window=config.chat.context_window,
            summarization_threshold=config.chat.summarization_threshold,
            ooc_protection=config.chat.ooc_protection,
            image_autonomy=config.chat.image_autonomy,
            image_autonomy_cooldown=config.chat.image_autonomy_cooldown,
            statistics_service=stats_svc,
            media_service=media_svc,
            channel="telegram",
            channel_instance_id=bot_id or "telegram",
            external_user_id=user_id,
            external_chat_id=chat_id,
        )

        # image_bytes from an incoming photo are not forwarded to the LLM
        # (ChatService does not support raw vision input).  The "[image]"
        # placeholder set by the caller already indicates that a photo was
        # received.
        result = await svc.generate_reply(
            conversation_id=conv_id,
            content=text,
            options=GenerationOptions(
                narration_mode="dialogue_only" if dialogue_only else "full",
            ),
        )
        return result
