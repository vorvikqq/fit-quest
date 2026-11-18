"""Тестовий «Telegram»: підміна сесії + хелпери, що надсилають апдейти в Dispatcher."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from itertools import count

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.app import build_dispatcher
from bot.config import PROJECT_ROOT
from bot.content.loader import load_and_validate
from bot.handlers import admin, game, registration
from bot.services.broadcast import Broadcaster

ADMIN_ID = 1000


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = count(1)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText)):
            return True
        chat_id = getattr(method, "chat_id", 0)
        return Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id if isinstance(chat_id, int) else 0, type="private"),
            text=getattr(method, "text", None),
        )

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


class Harness:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        admin_ids=(ADMIN_ID,),
        throttle_interval: float = 0.0,  # у тестах кнопки тиснуться підряд
    ):
        self.session = RecordingSession()
        self.bot = Bot("123456:TEST", session=self.session)
        self.content, self.report = load_and_validate(PROJECT_ROOT / "content" / "quest.yaml")
        # Роутери модульні (по одному на процес), а aiogram не дозволяє підключити їх до
        # другого Dispatcher. У проді Dispatcher один; тестам потрібен свіжий на кожен тест.
        for router in (admin.router, game.router, registration.router):
            router._parent_router = None
        self.dp = build_dispatcher(
            content=self.content,
            content_report=self.report,
            content_hash="hash-123",
            session_factory=session_factory,
            admin_ids=list(admin_ids),
            broadcaster=Broadcaster(self.bot, session_factory, rate=10_000),
            throttle_interval=throttle_interval,
        )
        self._update_ids = count(1)
        self._msg_ids = count(1000)

    # --- вхідні апдейти ---------------------------------------------------

    @staticmethod
    def _tg_user(user_id: int, username: str | None) -> User:
        return User(
            id=user_id, is_bot=False, first_name=f"Ім{user_id}", username=username or f"u{user_id}"
        )

    async def send(self, user_id: int, text: str, username: str | None = None) -> None:
        update = Update(
            update_id=next(self._update_ids),
            message=Message(
                message_id=next(self._msg_ids),
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=self._tg_user(user_id, username),
                text=text,
            ),
        )
        await self.dp.feed_update(self.bot, update)

    async def press(self, user_id: int, data: str) -> None:
        update = Update(
            update_id=next(self._update_ids),
            callback_query=CallbackQuery(
                id=str(next(self._update_ids)),
                from_user=self._tg_user(user_id, None),
                chat_instance="ci",
                data=data,
                message=Message(
                    message_id=next(self._msg_ids),
                    date=datetime.now(UTC),
                    chat=Chat(id=user_id, type="private"),
                    text="кнопки",
                ),
            ),
        )
        await self.dp.feed_update(self.bot, update)

    # --- що бот надіслав --------------------------------------------------

    def sent(self, to: int | None = None) -> list[SendMessage]:
        msgs = [c for c in self.session.calls if isinstance(c, SendMessage)]
        return [m for m in msgs if to is None or m.chat_id == to]

    def texts(self, to: int | None = None) -> list[str]:
        return [m.text for m in self.sent(to)]

    def last_text(self, to: int) -> str:
        return self.texts(to)[-1]

    def edits(self) -> list[EditMessageText]:
        return [c for c in self.session.calls if isinstance(c, EditMessageText)]

    def clear(self) -> None:
        self.session.calls.clear()

    def button_data(self, to: int) -> list[str]:
        """callback_data усіх кнопок останнього повідомлення з клавіатурою для `to`."""
        for m in reversed(self.sent(to)):
            if m.reply_markup:
                return [b.callback_data for row in m.reply_markup.inline_keyboard for b in row]
        return []

    async def wait_background(self) -> None:
        from bot.handlers import admin

        if admin.background_tasks:
            await asyncio.gather(*list(admin.background_tasks))
