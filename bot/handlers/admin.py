"""Адмін-команди (DESIGN §10). Доступ лише для ADMIN_IDS (AdminOnlyMiddleware).

Кожен перехід етапу просить підтвердження кнопками ✅/❌ (§3.1, FR-24). Підтвердження
перевіряє умови повторно під блокуванням, тож застаріла чи подвійна кнопка нічого не зламає.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.content.loader import Report
from bot.content.models import Content
from bot.db import repo
from bot.keyboards import AdminAct, Confirm, admin_panel_kb, confirm_kb, start_game_kb
from bot.middlewares import AdminOnlyMiddleware
from bot.services import phases
from bot.services.broadcast import Broadcaster

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

# Фонові розсилки: тримаємо посилання, щоб задачі не збирав GC.
background_tasks: set[asyncio.Task] = set()

SETCODE_MIN, SETCODE_MAX = 2, 64


# ---------------------------------------------------------------------------
# Панель
# ---------------------------------------------------------------------------


async def _render_panel(
    session_factory: async_sessionmaker[AsyncSession], content: Content, report: Report
) -> str:
    async with session_factory() as session:
        state = await repo.get_game_state(session)
        counters = await repo.get_counters(session)
    return texts.panel(state, counters, content.question_count, len(report.todos))


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    content: Content,
    content_report: Report,
) -> None:
    text = await _render_panel(session_factory, content, content_report)
    await message.answer(text, reply_markup=admin_panel_kb())


@router.callback_query(AdminAct.filter(F.action == "refresh"))
async def on_refresh(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    content: Content,
    content_report: Report,
) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        text = await _render_panel(session_factory, content, content_report)
        # «message is not modified», якщо нічого не змінилось — це не помилка
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(text, reply_markup=admin_panel_kb())


# ---------------------------------------------------------------------------
# /setcode
# ---------------------------------------------------------------------------


@router.message(Command("setcode"))
async def cmd_setcode(
    message: Message, command: CommandObject, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    code = (command.args or "").strip()
    if not SETCODE_MIN <= len(code) <= SETCODE_MAX:
        await message.answer(texts.ADMIN_USAGE_SETCODE)
        return
    async with session_factory.begin() as session:
        await repo.update_game_state(session, access_code=code)
    await message.answer(texts.code_changed(code))


# ---------------------------------------------------------------------------
# Запит підтвердження
# ---------------------------------------------------------------------------


async def _ask_confirm(
    bot: Bot,
    chat_id: int,
    action: phases.Action,
    force: bool,
    session_factory: async_sessionmaker[AsyncSession],
    report: Report,
) -> None:
    todo_count = len(report.todos)
    async with session_factory() as session:
        state = await repo.get_game_state(session)
        counters = await repo.get_counters(session)
    try:
        phases.preflight(action, state, todo_count=todo_count, force=force)
    except phases.PhaseConflict as exc:
        await bot.send_message(chat_id, texts.conflict(str(exc)))
        return
    await bot.send_message(
        chat_id,
        texts.confirm(action, counters.registered, todo_count, force, state.access_code),
        reply_markup=confirm_kb(action, force),
    )


@router.message(Command("open_reg"))
async def cmd_open_reg(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "open_reg", False, session_factory, content_report)


@router.message(Command("close_reg"))
async def cmd_close_reg(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "close_reg", False, session_factory, content_report)


@router.message(Command("start_quest"))
async def cmd_start_quest(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    arg = (command.args or "").strip().lower()
    if arg not in ("", "force"):
        await message.answer(texts.ADMIN_USAGE_START_QUEST)
        return
    await _ask_confirm(
        bot, message.chat.id, "start_quest", arg == "force", session_factory, content_report
    )


@router.callback_query(AdminAct.filter(F.action.in_({"open_reg", "close_reg", "start_quest"})))
async def on_panel_action(
    callback: CallbackQuery,
    callback_data: AdminAct,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await callback.answer()
    await _ask_confirm(
        bot,
        callback.from_user.id,
        callback_data.action,  # type: ignore[arg-type]
        False,
        session_factory,
        content_report,
    )


# ---------------------------------------------------------------------------
# Виконання після підтвердження
# ---------------------------------------------------------------------------


async def _finish_message(callback: CallbackQuery, text: str) -> None:
    """Замінює повідомлення з кнопками результатом (кнопки зникають, повторно не натиснеш)."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(text, reply_markup=None)
            return
    await callback.bot.send_message(callback.from_user.id, text)  # type: ignore[union-attr]


async def _broadcast_quest_started(
    broadcaster: Broadcaster, recipients: list[int], content: Content, admin_chat_id: int
) -> None:
    try:
        result = await broadcaster.send(
            recipients, content.messages["quest_started"], reply_markup=start_game_kb(content)
        )
        await broadcaster.bot.send_message(admin_chat_id, texts.broadcast_done(result))
    except Exception:  # noqa: BLE001
        log.exception("Розсилка «квест стартував» впала")


@router.callback_query(Confirm.filter())
async def on_confirm(
    callback: CallbackQuery,
    callback_data: Confirm,
    session_factory: async_sessionmaker[AsyncSession],
    content: Content,
    content_report: Report,
    content_hash: str,
    broadcaster: Broadcaster,
) -> None:
    await callback.answer()
    if not callback_data.yes:
        await _finish_message(callback, texts.CANCELLED)
        return

    action = callback_data.action
    try:
        recipients: list[int] = []
        async with session_factory.begin() as session:
            if action == "open_reg":
                await phases.open_registration(session)
            elif action == "close_reg":
                await phases.close_registration(session)
            elif action == "start_quest":
                await phases.start_quest(
                    session,
                    content_hash=content_hash,
                    now=datetime.now(UTC),
                    todo_count=len(content_report.todos),
                    force=callback_data.force,
                )
                recipients = await repo.list_registered_ids(session)
            else:
                return
    except phases.PhaseConflict as exc:
        await _finish_message(callback, texts.conflict(str(exc)))
        return

    await _finish_message(callback, texts.done(action, len(recipients)))

    if action == "start_quest":
        # Розсилка в фоні: підтвердження не чекає 8+ секунд на 200 учасників.
        task = asyncio.create_task(
            _broadcast_quest_started(broadcaster, recipients, content, callback.from_user.id)
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
