"""Після гри: відгук одним реченням (DESIGN §6.6, FR-20).

Стан зберігається в БД: `attempts.feedback IS NULL` означає «ще не відповів». Тому відгук
переживає рестарт бота, а FSM для нього не потрібен. «Пропустити» пише порожній рядок.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.db.models import AttemptStatus
from bot.keyboards import FEEDBACK_SKIP_DATA

router = Router(name="after_game")


async def _awaiting_feedback(
    message: Message, session_factory: async_sessionmaker[AsyncSession]
) -> bool:
    if message.from_user is None:
        return False
    async with session_factory() as session:
        attempt = await repo.get_attempt_for_user(session, message.from_user.id)
    return (
        attempt is not None
        and attempt.status == AttemptStatus.FINISHED
        and attempt.feedback is None
    )


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"), _awaiting_feedback)
async def on_feedback(
    message: Message, session_factory: async_sessionmaker[AsyncSession], content: Content
) -> None:
    text = " ".join((message.text or "").split())
    async with session_factory.begin() as session:
        saved = await repo.set_feedback(session, message.from_user.id, text)  # type: ignore[union-attr]
    if saved:
        await message.answer(content.messages["feedback_thanks"])


@router.callback_query(F.data == FEEDBACK_SKIP_DATA)
async def on_skip(
    callback: CallbackQuery, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory.begin() as session:
        await repo.set_feedback(session, callback.from_user.id, repo.FEEDBACK_SKIPPED)
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)
