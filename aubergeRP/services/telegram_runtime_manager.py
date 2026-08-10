"""TelegramRuntimeManager — runs multiple aiogram bots concurrently.

Design
------
- Each enabled bot runs in its own asyncio Task using aiogram's polling.
- Bots are isolated: stopping one does not affect others.
- Generation is serialized *per-conversation* using per-key asyncio.Lock stored
  in a dict.  Different users on the same bot, and same user across different
  bots, all run independently.
- Tokens are never logged.

Private-chat-only MVP
---------------------
Group and supergroup messages are silently ignored.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher
    from aiogram.types import Message

logger = logging.getLogger(__name__)

# Maximum Telegram message length (Bot API limit).
_TG_MAX_LEN = 4096


def split_message(text: str, max_len: int = _TG_MAX_LEN) -> list[str]:
    """Split a long text into chunks ≤ max_len, preferring paragraph breaks."""
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
        remaining = remaining[idx:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts


class TelegramRuntimeManager:
    """Manages the lifecycle of one aiogram Dispatcher per Telegram bot."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        # bot_id → asyncio.Task
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # per-conversation asyncio.Lock for serialized generation
        self._conv_locks: dict[str, asyncio.Lock] = {}

    # ── Public interface ──────────────────────────────────────────────────────

    async def start_enabled_bots(self) -> None:
        from .telegram_bot_service import TelegramBotService
        svc = TelegramBotService(self._data_dir)
        for summary, token in svc.list_enabled_bots_with_tokens():
            await self.start_bot(summary.id, token, summary.character_id)

    async def start_bot(self, bot_id: str, token: str, character_id: str) -> None:
        if bot_id in self._tasks and not self._tasks[bot_id].done():
            return
        task = asyncio.create_task(
            self._run_bot(bot_id, token, character_id),
            name=f"telegram-bot-{bot_id}",
        )
        self._tasks[bot_id] = task

    async def stop_bot(self, bot_id: str) -> None:
        task = self._tasks.pop(bot_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

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
            await self.start_bot(bot_id, token, summary.character_id)

    async def stop_all(self) -> None:
        for bot_id in list(self._tasks):
            await self.stop_bot(bot_id)

    def is_running(self, bot_id: str) -> bool:
        task = self._tasks.get(bot_id)
        return task is not None and not task.done()

    # ── Internal: run one bot ─────────────────────────────────────────────────

    async def _run_bot(self, bot_id: str, token: str, character_id: str) -> None:
        try:
            from aiogram import Bot, Dispatcher
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode

            bot = Bot(
                token=token,
                default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
            )
            dp = Dispatcher()

            self._register_handlers(dp, bot_id, character_id, bot)

            logger.info("Telegram bot %s: starting polling", bot_id)
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            logger.info("Telegram bot %s: polling stopped", bot_id)
        except Exception:
            logger.exception("Telegram bot %s: unexpected error, polling stopped", bot_id)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _register_handlers(
        self,
        dp: Dispatcher,
        bot_id: str,
        character_id: str,
        bot: Bot,
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
            conv_id, created = await asyncio.get_event_loop().run_in_executor(
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
            await asyncio.get_event_loop().run_in_executor(
                None, self._reset_session, bot_id, user_id, chat_id, character_id
            )
            await message.answer("🔄 Conversation reset. Starting fresh!")

        @dp.message(Command("status"))
        async def on_status(message: Message) -> None:
            if not self._is_private(message):
                return
            # Safe info only — no token, no internal IDs
            from .character_service import CharacterService
            from .telegram_bot_service import TelegramBotService
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
            status_text = (
                f"🤖 Bot: {summary.name}\n"
                f"👤 Character: {char_name}\n"
                f"🟢 Status: {'Running' if self.is_running(bot_id) else 'Stopped'}"
            )
            await message.answer(status_text)

        @dp.message(F.chat.type == "private")
        async def on_message(message: Message) -> None:
            text = message.text or message.caption or ""
            if not text.strip():
                return
            user_id = str(message.from_user.id) if message.from_user else "0"
            chat_id = str(message.chat.id)

            conv_id, _ = await asyncio.get_event_loop().run_in_executor(
                None, self._get_or_create_session, bot_id, user_id, chat_id, character_id
            )

            # Serialize generation per conversation
            lock = self._get_conv_lock(conv_id)
            async with lock:
                try:
                    reply_text = await self._generate(conv_id, text)
                except Exception:
                    logger.exception("Telegram bot %s: generation failed for conv %s", bot_id, conv_id)
                    await message.answer("⚠️ Generation failed. Please try again.")
                    return

            # Deliver (split if needed) — failure does NOT re-generate
            chunks = split_message(reply_text)
            for chunk in chunks:
                try:
                    await message.answer(chunk)
                except Exception:
                    logger.error(
                        "Telegram bot %s: delivery failed for conv %s (chunk len=%d)",
                        bot_id, conv_id, len(chunk),
                    )
                    break

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
        return svc.get_or_create(
            channel="telegram",
            channel_instance_id=bot_id,
            external_user_id=user_id,
            external_chat_id=chat_id,
            character_id=character_id,
        )

    def _reset_session(
        self,
        bot_id: str,
        user_id: str,
        chat_id: str,
        character_id: str,
    ) -> str:
        from .channel_session_service import ChannelSessionService
        svc = ChannelSessionService(self._data_dir)
        return svc.reset(
            channel="telegram",
            channel_instance_id=bot_id,
            external_user_id=user_id,
            external_chat_id=chat_id,
            character_id=character_id,
        )

    async def _generate(self, conv_id: str, text: str) -> str:
        from ..config import get_config
        from ..connectors.manager import ConnectorManager
        from ..services.character_service import CharacterService
        from ..services.chat_service import ChatService, GenerationOptions
        from ..services.conversation_service import ConversationService
        from ..services.media_service import MediaService
        from ..services.statistics_service import StatisticsService

        config = get_config()
        data_dir = config.app.data_dir
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
            statistics_service=stats_svc,
            media_service=media_svc,
        )
        result = await svc.generate_reply(
            conversation_id=conv_id,
            content=text,
            options=GenerationOptions(),
        )
        return result.text
