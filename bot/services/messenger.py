"""Відправка повідомлень гри. Ігрове ядро знає лише протокол `Messenger`, не aiogram.

Це дозволяє тестувати `GameService` без Telegram і ганяти навантажувальний тест
на 200 «учасників» без мережі (DESIGN §15.2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.keyboards import answer_kb, card_kb, skip_feedback_kb
from bot.services.media import MediaService
from bot.services.render import QuestionView

log = logging.getLogger(__name__)
T = TypeVar("T")


class Messenger(Protocol):
    """Усі методи повертають `message_id` або `None`, якщо надіслати не вдалося
    (користувач заблокував бота тощо). Гра від цього не зупиняється."""

    async def send_text(self, tg_id: int, text: str) -> int | None: ...

    async def send_card(self, tg_id: int, text: str, cursor: int) -> int | None: ...

    async def send_question(self, tg_id: int, view: QuestionView, cursor: int) -> int | None: ...

    async def send_feedback_prompt(self, tg_id: int, text: str) -> int | None: ...

    async def close_question(
        self, tg_id: int, message_id: int | None, view: QuestionView, suffix: str
    ) -> None: ...

    async def remove_keyboard(self, tg_id: int, message_id: int | None) -> None: ...


class TelegramMessenger:
    def __init__(
        self,
        bot: Bot,
        content: Content,
        media: MediaService,
        session_factory: async_sessionmaker[AsyncSession],
        retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self.content = content
        self.media = media
        self._session_factory = session_factory
        self._retries = retries
        self._sleep = sleep

    # --- обгортка з обробкою типових збоїв ---------------------------------

    async def _call(
        self, tg_id: int, make: Callable[[], Awaitable[T]], *, reraise_bad_request: bool = False
    ) -> T | None:
        for _ in range(self._retries + 1):
            try:
                return await make()
            except TelegramBadRequest as exc:
                if reraise_bad_request:
                    raise
                log.error("Telegram помилка для %s: %s", tg_id, exc)
                return None
            except TelegramRetryAfter as exc:
                log.warning("RetryAfter %s с (чат %s)", exc.retry_after, tg_id)
                await self._sleep(exc.retry_after)
            except TelegramForbiddenError:
                async with self._session_factory.begin() as session:
                    await repo.set_user_flag(session, tg_id, blocked_bot=True)
                return None
            except TelegramAPIError as exc:
                log.error("Telegram помилка для %s: %s", tg_id, exc)
                return None
        log.error("Вичерпано повтори для %s", tg_id)
        return None

    # --- Messenger ---------------------------------------------------------

    async def send_text(self, tg_id: int, text: str) -> int | None:
        msg = await self._call(tg_id, lambda: self.bot.send_message(tg_id, text))
        return msg.message_id if msg else None

    async def send_card(self, tg_id: int, text: str, cursor: int) -> int | None:
        markup = card_kb(self.content, cursor)
        msg = await self._call(
            tg_id, lambda: self.bot.send_message(tg_id, text, reply_markup=markup)
        )
        return msg.message_id if msg else None

    async def send_feedback_prompt(self, tg_id: int, text: str) -> int | None:
        markup = skip_feedback_kb(self.content)
        msg = await self._call(
            tg_id, lambda: self.bot.send_message(tg_id, text, reply_markup=markup)
        )
        return msg.message_id if msg else None

    async def _send_photo(
        self, tg_id: int, rel: str, caption: str | None, markup: InlineKeyboardMarkup | None
    ) -> int | None:
        """Спершу кешований file_id; якщо його нема чи він не підійшов — вантажимо файл і кешуємо."""
        file_id = await self.media.cached_file_id(rel)
        if file_id is not None:
            try:
                msg = await self._call(
                    tg_id,
                    lambda: self.bot.send_photo(tg_id, file_id, caption=caption, reply_markup=markup),
                    reraise_bad_request=True,
                )
                return msg.message_id if msg else None
            except TelegramBadRequest:
                log.warning("file_id для %s не підійшов, вантажу файл", rel)

        upload = FSInputFile(self.media.path(rel))
        msg = await self._call(
            tg_id,
            lambda: self.bot.send_photo(tg_id, upload, caption=caption, reply_markup=markup),
        )
        if msg is None:
            return None
        if msg.photo:
            await self.media.remember(rel, msg.photo[-1].file_id)
        return msg.message_id

    async def send_question(self, tg_id: int, view: QuestionView, cursor: int) -> int | None:
        markup = answer_kb(view.buttons, cursor)
        if view.image is None:
            msg = await self._call(
                tg_id, lambda: self.bot.send_message(tg_id, view.text, reply_markup=markup)
            )
            return msg.message_id if msg else None
        if not view.split:
            return await self._send_photo(tg_id, view.image, view.text, markup)
        # Підпис задовгий (>1024): фото окремо, питання з кнопками — наступним (§6.3).
        await self._send_photo(tg_id, view.image, None, None)
        msg = await self._call(
            tg_id, lambda: self.bot.send_message(tg_id, view.text, reply_markup=markup)
        )
        return msg.message_id if msg else None

    async def close_question(
        self, tg_id: int, message_id: int | None, view: QuestionView, suffix: str
    ) -> None:
        """Прибирає кнопки й дописує підтвердження (без правильності, FR-12)."""
        if message_id is None:
            return
        text = f"{view.text}\n\n{suffix}"
        try:
            if view.caption_mode:
                await self.bot.edit_message_caption(
                    chat_id=tg_id, message_id=message_id, caption=text, reply_markup=None
                )
            else:
                await self.bot.edit_message_text(
                    text, chat_id=tg_id, message_id=message_id, reply_markup=None
                )
        except TelegramRetryAfter as exc:
            await self._sleep(exc.retry_after)
            await self.remove_keyboard(tg_id, message_id)
        except TelegramAPIError:
            # Наприклад, підпис із припискою перевищив 1024: бодай приберемо кнопки.
            await self.remove_keyboard(tg_id, message_id)

    async def remove_keyboard(self, tg_id: int, message_id: int | None) -> None:
        if message_id is None:
            return
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=tg_id, message_id=message_id, reply_markup=None
            )
        except TelegramAPIError as exc:
            log.debug("Не вдалося прибрати клавіатуру %s/%s: %s", tg_id, message_id, exc)
