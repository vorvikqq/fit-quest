"""Хендлери гри: «Почати квест», «Почати раунд», відповіді (DESIGN §6)."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.db.models import AttemptStatus
from bot.keyboards import START_GAME_DATA, AnsCb, GoCb
from bot.services.game import GameService

router = Router(name="game")

ACTIVE = (AttemptStatus.ROUND_INTRO, AttemptStatus.QUESTION)


@router.callback_query(F.data == START_GAME_DATA)
async def on_start_quest(callback: CallbackQuery, game: GameService, content: Content) -> None:
    result = await game.start_or_resume(callback.from_user.id)
    alert = {
        "not_started": content.messages["quest_not_started"],
        "closed": content.messages["quest_closed_late"],
    }.get(result)
    await callback.answer(alert, show_alert=alert is not None)
    if result in ("started", "resumed") and isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(GoCb.filter())
async def on_go(callback: CallbackQuery, callback_data: GoCb, game: GameService) -> None:
    await game.go(callback.from_user.id, callback_data.cursor)
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(AnsCb.filter())
async def on_answer(
    callback: CallbackQuery, callback_data: AnsCb, game: GameService, content: Content
) -> None:
    accepted = await game.answer(callback.from_user.id, callback_data.cursor, callback_data.btn)
    # Відповідь уже зараховано / застаріла кнопка (NFR-4): коротке сповіщення замість тиші.
    await callback.answer(None if accepted else content.messages["answer_stale"])


async def _in_game(message: Message, session_factory: async_sessionmaker[AsyncSession]) -> bool:
    if message.from_user is None:
        return False
    async with session_factory() as session:
        attempt = await repo.get_attempt_for_user(session, message.from_user.id)
    return attempt is not None and attempt.status in ACTIVE


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"), _in_game)
async def text_during_game(message: Message, content: Content) -> None:
    await message.answer(content.messages["use_buttons"])
