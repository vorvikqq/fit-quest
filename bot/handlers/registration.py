"""Реєстрація (DESIGN §3.2): /start → код → ім'я → статус → [курс] → REGISTERED.

FSM aiogram зберігає лише кроки реєстрації й живе в пам'яті. Якщо бот перезапустився
посеред реєстрації, учасник, що пише текст без стану, повертається на початок (`fallback`).
Ігровий стан тут не зберігається, він живе в `attempts`.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.db.models import COURSES, GameState, Phase, User, UserStatus
from bot.keyboards import CourseCb, StatusCb, course_kb, start_game_kb, status_kb
from bot.services import phases
from bot.services.ratelimit import SlidingWindowLimiter

router = Router(name="registration")
router.message.filter(F.chat.type == "private")

NAME_MIN, NAME_MAX = 2, 60
_URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.IGNORECASE)


class Reg(StatesGroup):
    code = State()
    name = State()
    status = State()
    course = State()


def clean_name(raw: str) -> str | None:
    """DESIGN §13: прибираємо URL і `@`, стискаємо пробіли, обрізаємо до 60. `None`, якщо < 2."""
    name = _URL_RE.sub(" ", raw).replace("@", " ")
    name = " ".join(name.split())[:NAME_MAX].strip()
    return name if len(name) >= NAME_MIN else None


def normalize_code(code: str) -> str:
    return code.strip().casefold()


# ---------------------------------------------------------------------------
# Допоміжне
# ---------------------------------------------------------------------------


async def _game_state(session_factory: async_sessionmaker[AsyncSession]) -> GameState:
    async with session_factory() as session:
        return await repo.get_game_state(session)


async def _show_registered(bot: Bot, chat_id: int, content: Content, state: GameState) -> None:
    """Екран REGISTERED: чекаємо старту або одразу даємо кнопку «Почати» (етап RUNNING)."""
    if state.phase == Phase.RUNNING:
        await bot.send_message(
            chat_id, content.messages["quest_started"], reply_markup=start_game_kb(content)
        )
    else:
        await bot.send_message(chat_id, content.messages["registered_waiting"])


async def _clear_markup(callback: CallbackQuery) -> None:
    """Прибирає кнопки з уже відповіданого питання (best effort)."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(reply_markup=None)


async def _ask_code_result(
    bot: Bot,
    user: User,
    text: str,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    code_limiter: SlidingWindowLimiter,
) -> None:
    """Перевіряє кодове слово: спільна логіка для кроку AWAIT_CODE та deep link."""
    if not code_limiter.hit(user.tg_id):
        await bot.send_message(user.tg_id, content.messages["too_many_attempts"])
        return

    game = await _game_state(session_factory)
    if not phases.registration_allowed(game):
        await state.clear()
        await bot.send_message(user.tg_id, content.messages["registration_closed"])
        return

    expected = normalize_code(game.access_code)
    if expected and normalize_code(text) == expected:
        code_limiter.reset(user.tg_id)
        await bot.send_message(user.tg_id, content.messages["consent_note"])  # FR-5
        await bot.send_message(user.tg_id, content.messages["ask_name"])
        await state.set_state(Reg.name)
    else:
        await bot.send_message(user.tg_id, content.messages["wrong_code"])
        await state.set_state(Reg.code)


async def _begin(
    bot: Bot,
    user: User,
    code: str,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    code_limiter: SlidingWindowLimiter,
) -> None:
    """Початок для незареєстрованого: закрито / deep-link з кодом / запит коду."""
    await state.clear()
    game = await _game_state(session_factory)
    if not phases.registration_allowed(game):
        await bot.send_message(user.tg_id, content.messages["registration_closed"])
        return
    if code:
        await _ask_code_result(bot, user, code, state, content, session_factory, code_limiter)
        return
    await bot.send_message(user.tg_id, content.messages["welcome"])
    await state.set_state(Reg.code)


# ---------------------------------------------------------------------------
# /start та крок AWAIT_CODE
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    code_limiter: SlidingWindowLimiter,
) -> None:
    # Зареєстрований повертається туди, де зупинився (гру додамо разом з game.py).
    if user.registered_at is not None:
        await state.clear()
        await _show_registered(bot, user.tg_id, content, await _game_state(session_factory))
        return
    await _begin(
        bot, user, (command.args or "").strip(), state, content, session_factory, code_limiter
    )


@router.message(Reg.code, F.text)
async def step_code(
    message: Message,
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    code_limiter: SlidingWindowLimiter,
) -> None:
    await _ask_code_result(
        bot, user, message.text or "", state, content, session_factory, code_limiter
    )


# ---------------------------------------------------------------------------
# AWAIT_NAME
# ---------------------------------------------------------------------------


@router.message(Reg.name, F.text)
async def step_name(message: Message, bot: Bot, user: User, state: FSMContext, content: Content) -> None:
    name = clean_name(message.text or "")
    if name is None:
        await bot.send_message(user.tg_id, content.messages["invalid_name"])
        return
    await state.update_data(full_name=name)
    await state.set_state(Reg.status)
    await bot.send_message(user.tg_id, content.messages["ask_status"], reply_markup=status_kb(content))


@router.message(Reg.name)
async def step_name_not_text(message: Message, bot: Bot, user: User, content: Content) -> None:
    await bot.send_message(user.tg_id, content.messages["invalid_name"])


# ---------------------------------------------------------------------------
# AWAIT_STATUS / AWAIT_COURSE
# ---------------------------------------------------------------------------


@router.callback_query(Reg.status, StatusCb.filter())
async def step_status(
    callback: CallbackQuery,
    callback_data: StatusCb,
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await callback.answer()
    if callback_data.value not in {s.value for s in UserStatus}:
        return
    await _clear_markup(callback)
    await state.update_data(status=callback_data.value)

    if callback_data.value == UserStatus.FIT_STUDENT:
        await state.set_state(Reg.course)
        await bot.send_message(
            user.tg_id, content.messages["ask_course"], reply_markup=course_kb(content)
        )
    else:
        await _finish(bot, user, state, content, session_factory, course=None)


@router.callback_query(Reg.course, CourseCb.filter())
async def step_course(
    callback: CallbackQuery,
    callback_data: CourseCb,
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await callback.answer()
    if callback_data.value not in COURSES:
        return
    await _clear_markup(callback)
    await _finish(bot, user, state, content, session_factory, course=callback_data.value)


@router.message(Reg.status)
async def status_text(message: Message, bot: Bot, user: User, content: Content) -> None:
    await bot.send_message(user.tg_id, content.messages["ask_status"], reply_markup=status_kb(content))


@router.message(Reg.course)
async def course_text(message: Message, bot: Bot, user: User, content: Content) -> None:
    await bot.send_message(user.tg_id, content.messages["ask_course"], reply_markup=course_kb(content))


async def _finish(
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    course: str | None,
) -> None:
    data = await state.get_data()
    await state.clear()

    async with session_factory.begin() as session:
        game = await repo.get_game_state(session)
        closed = not phases.registration_allowed(game)  # могли закрити, поки учасник вводив дані
        if not closed:
            # False = уже зареєстрований (подвійний клік): не страшно, просто покажемо екран.
            await repo.register_user(
                session,
                tg_id=user.tg_id,
                full_name=data["full_name"],
                status=data["status"],
                course=course,
                now=datetime.now(UTC),
            )
    if closed:
        await bot.send_message(user.tg_id, content.messages["registration_closed"])
        return
    await _show_registered(bot, user.tg_id, content, game)


# ---------------------------------------------------------------------------
# Текст без стану (наприклад, після рестарту посеред реєстрації)
# ---------------------------------------------------------------------------


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def fallback(
    message: Message,
    bot: Bot,
    user: User,
    state: FSMContext,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    code_limiter: SlidingWindowLimiter,
) -> None:
    if user.registered_at is not None:
        return  # зареєстрованих тут нічим не турбуємо; гра обробляється окремо
    await _begin(bot, user, "", state, content, session_factory, code_limiter)
