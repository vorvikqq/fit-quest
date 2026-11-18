"""/start-заглушка: без мережі, з підміненою сесією Telegram, на справжній БД."""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User

from bot.config import PROJECT_ROOT
from bot.content.loader import load_content
from bot.db import repo
from bot.handlers import registration


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        return Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=42, type="private"), text="ok"
        )

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        pass


async def test_start_replies_with_welcome_and_saves_user(session_factory):
    content = load_content(PROJECT_ROOT / "content" / "quest.yaml")
    session = RecordingSession()
    bot = Bot("123456:TEST", session=session)

    dp = Dispatcher()
    dp["content"] = content
    dp["session_factory"] = session_factory
    dp["admin_ids"] = [42]
    dp.include_router(registration.router)

    tg_user = User(id=42, is_bot=False, first_name="Іван", last_name="Петренко", username="ivan")
    update = Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=42, type="private"),
            from_user=tg_user,
            text="/start",
        ),
    )
    await dp.feed_update(bot, update)

    (reply,) = session.sent
    assert isinstance(reply, SendMessage)
    assert reply.chat_id == 42 and reply.text == content.messages["welcome"]

    async with session_factory() as s:
        user = await repo.get_user(s, 42)
    assert (user.username, user.tg_name, user.is_admin) == ("ivan", "Іван Петренко", True)
    assert user.registered_at is None  # реєстрація — наступний крок
