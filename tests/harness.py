"""Тестовий «Telegram»: підміна сесії + хелпери, що надсилають апдейти в Dispatcher."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from itertools import count

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.app import build_dispatcher
from bot.config import PROJECT_ROOT
from bot.content.loader import load_and_validate, validate_content
from bot.handlers import admin, admin_tools, after_game, registration
from bot.handlers import game as game_handlers
from bot.services.broadcast import Broadcaster
from bot.services.game import GameService
from bot.services.media import MediaService
from bot.services.messenger import TelegramMessenger

ADMIN_ID = 1000

_EDITS = (AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, EditMessageCaption)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = count(1)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, _EDITS):
            return True
        chat_id = getattr(method, "chat_id", 0)
        photo = None
        if isinstance(method, SendPhoto):  # Telegram віддає file_id завантаженого фото
            photo = [PhotoSize(file_id=f"FID-{len(self.calls)}", file_unique_id="u", width=1, height=1)]
        return Message(
            message_id=next(self._ids),
            date=datetime.now(UTC),
            chat=Chat(id=chat_id if isinstance(chat_id, int) else 0, type="private"),
            text=getattr(method, "text", None),
            photo=photo,
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
        content=None,
        content_path=None,
        board_url="http://board.test",
        board_token="board-secret",
    ):
        self.session = RecordingSession()
        self.bot = Bot("123456:TEST", session=self.session)
        self.content, self.report = load_and_validate(PROJECT_ROOT / "content" / "quest.yaml")
        if content is not None:  # синтетичний контент без TODO для наскрізних сценаріїв
            self.content, self.report = content, validate_content(content, PROJECT_ROOT / "content")
        # Роутери модульні (по одному на процес), а aiogram не дозволяє підключити їх до
        # другого Dispatcher. У проді Dispatcher один; тестам потрібен свіжий на кожен тест.
        for router in (admin.router, admin_tools.router, game_handlers.router, after_game.router, registration.router):
            router._parent_router = None
        media = MediaService(session_factory, PROJECT_ROOT / "content")
        self.media = media
        self.game = GameService(
            session_factory,
            self.content,
            TelegramMessenger(self.bot, self.content, media, session_factory),
        )
        self.dp = build_dispatcher(
            content=self.content,
            content_report=self.report,
            content_hash="hash-123",
            session_factory=session_factory,
            admin_ids=list(admin_ids),
            broadcaster=Broadcaster(self.bot, session_factory, rate=10_000),
            game=self.game,
            media=media,
            content_path=content_path or PROJECT_ROOT / "content" / "quest.yaml",
            board_url=board_url,
            board_token=board_token,
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

    def documents(self, to: int | None = None) -> list[SendDocument]:
        calls = [c for c in self.session.calls if isinstance(c, SendDocument)]
        return [c for c in calls if to is None or c.chat_id == to]

    def photos(self, to: int | None = None) -> list[SendPhoto]:
        calls = [c for c in self.session.calls if isinstance(c, SendPhoto)]
        return [c for c in calls if to is None or c.chat_id == to]

    def caption_edits(self) -> list[EditMessageCaption]:
        return [c for c in self.session.calls if isinstance(c, EditMessageCaption)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [c for c in self.session.calls if isinstance(c, AnswerCallbackQuery)]

    def photo_then_question(self, to: int) -> bool:
        """Останнє фото для `to` іде безпосередньо перед питанням з кнопками."""
        seq = [
            c for c in self.session.calls if isinstance(c, (SendPhoto, SendMessage)) and c.chat_id == to
        ]
        return isinstance(seq[-2], SendPhoto) and isinstance(seq[-1], SendMessage)

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
        if admin.background_tasks:
            await asyncio.gather(*list(admin.background_tasks))
