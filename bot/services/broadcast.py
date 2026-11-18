"""Масові розсилки з урахуванням лімітів Telegram (DESIGN §9).

Усі масові події (старт, закриття, результати, /broadcast) ідуть лише тут.
Повідомлення відправляються послідовно з рівним інтервалом `1/rate` (25/с за
замовчуванням, ліміт Telegram ~30/с); на `RetryAfter` чекаємо й повторюємо,
на `Forbidden` ставимо `users.blocked_bot`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db import repo

log = logging.getLogger(__name__)


@dataclass
class BroadcastResult:
    sent: int = 0
    blocked: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.blocked + self.failed


class Broadcaster:
    def __init__(
        self,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        rate: float = 25.0,
        max_retries: int = 3,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self._session_factory = session_factory
        self._interval = 1.0 / rate
        self._max_retries = max_retries
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def _wait_turn(self) -> None:
        """Видає «слот» відправки; паралельні розсилки ділять один ліміт."""
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            if slot > now:
                await self._sleep(slot - now)

    async def _send_one(
        self, tg_id: int, text: str, reply_markup: InlineKeyboardMarkup | None
    ) -> str:
        for _ in range(self._max_retries + 1):
            await self._wait_turn()
            try:
                await self.bot.send_message(tg_id, text, reply_markup=reply_markup)
                return "sent"
            except TelegramRetryAfter as exc:
                log.warning("RetryAfter %s с для %s", exc.retry_after, tg_id)
                await self._sleep(exc.retry_after)
            except TelegramForbiddenError:
                async with self._session_factory.begin() as session:
                    await repo.set_user_flag(session, tg_id, blocked_bot=True)
                return "blocked"
            except TelegramAPIError as exc:
                log.error("Не вдалося надіслати %s: %s", tg_id, exc)
                return "failed"
        log.error("Вичерпано спроби для %s", tg_id)
        return "failed"

    async def send(
        self,
        recipients: Iterable[int],
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> BroadcastResult:
        result = BroadcastResult()
        for tg_id in recipients:
            outcome = await self._send_one(tg_id, text, reply_markup)
            setattr(result, outcome, getattr(result, outcome) + 1)
        log.info("Розсилка: %s", result)
        return result
