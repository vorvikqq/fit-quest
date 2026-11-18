"""Middleware: throttling, user-upsert, admin-check (DESIGN §2, §9, §10).

Порядок підключення (див. `bot/app.py`): throttling → user-upsert → хендлери;
admin-check стоїть на admin-роутері.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db import repo

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class ThrottlingMiddleware(BaseMiddleware):
    """Відкидає натискання кнопок частіше ніж раз на `min_interval` від одного користувача (§9).

    Відкинуте натискання відхиляється без дій, а спінер на кнопці знімається.
    Для звичайних повідомлень ліміту немає: їх стримують окремі обмежувачі (спроби коду).
    """

    def __init__(
        self, min_interval: float = 0.5, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._last: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        now = self._clock()
        last = self._last.get(tg_user.id)
        if last is not None and now - last < self.min_interval:
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(TelegramAPIError):
                    await event.answer()
            return None

        self._last[tg_user.id] = now
        if len(self._last) > 5000:  # не росте безмежно
            cutoff = now - 60
            self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return await handler(event, data)


class UserUpsertMiddleware(BaseMiddleware):
    """Кладе в БД/оновлює користувача й додає в дані хендлера `user` та `is_admin`."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], admin_ids: list[int]
    ) -> None:
        self._session_factory = session_factory
        self._admin_ids = set(admin_ids)

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        is_admin = tg_user.id in self._admin_ids
        async with self._session_factory.begin() as session:
            data["user"] = await repo.upsert_user(
                session,
                tg_id=tg_user.id,
                username=tg_user.username,
                tg_name=tg_user.full_name,
                is_admin=is_admin,
            )
        data["is_admin"] = is_admin
        return await handler(event, data)


class AdminOnlyMiddleware(BaseMiddleware):
    """Не-адмінів ігнорує мовчки: невідома команда від не-адміна без відповіді (§10)."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        if not data.get("is_admin"):
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(TelegramAPIError):
                    await event.answer()
            return None
        return await handler(event, data)
