"""Після гри: відгук одним реченням (DESIGN §6.6, FR-20).

Стан зберігається в БД: `attempts.feedback IS NULL` означає «ще не відповів». Тому відгук
переживає рестарт бота, а FSM для нього не потрібен. «Пропустити» пише порожній рядок.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.db.models import AttemptStatus, Phase
from bot.keyboards import FEEDBACK_SKIP_DATA
from bot.services.broadcast import Broadcaster
from bot.services.game import GameService
from bot.services.ratelimit import SlidingWindowLimiter
from bot.services.results import render_header, render_review

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


@router.message(Command("results", "review"), F.chat.type == "private")
async def cmd_results(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    content: Content,
    game: GameService,
    broadcaster: Broadcaster,
    review_limiter: SlidingWindowLimiter,
) -> None:
    """Власний розбір учасника: що відповів, що правильно й чому (лише після публікації результатів)."""
    tg_id = message.from_user.id  # type: ignore[union-attr]
    messages = content.messages

    async with session_factory() as session:
        state = await repo.get_game_state(session)
        attempt = await repo.get_attempt_for_user(session, tg_id)
        if state.phase == Phase.RESULTS and attempt is not None:
            answers = await repo.answers_by_attempt(session, [attempt.id])
            ranking = await repo.leaderboard(session)

    if state.phase != Phase.RESULTS:
        await message.answer(messages["review_not_published"])
        return
    if attempt is None or attempt.status not in (AttemptStatus.FINISHED, AttemptStatus.CLOSED_UNFINISHED):
        await message.answer(messages["review_no_attempt"])
        return
    if not review_limiter.hit(tg_id):  # захист від спаму: розбір це кілька повідомлень
        await message.answer(messages["review_slow_down"])
        return

    place = next((r.place for r in ranking if r.tg_id == tg_id), None)  # адмін/виключений: без місця
    items = [(tg_id, render_header(content, place, len(ranking), attempt.score, len(attempt.plan)))]
    items.extend((tg_id, chunk) for chunk in render_review(game.index, attempt, answers[attempt.id]))
    # Через загальну чергу розсилки: після оголошення результатів запити прийдуть від багатьох одразу.
    await broadcaster.send_personal(items)
