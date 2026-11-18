"""Адмін-команди (DESIGN §10). Доступ лише для ADMIN_IDS (AdminOnlyMiddleware).

Кожен перехід етапу й кожна масова розсилка просять підтвердження кнопками ✅/❌
(§3.1, FR-24). Підтвердження перевіряє умови повторно під блокуванням, тож застаріла чи
подвійна кнопка нічого не зламає. Усі масові відправки йдуть у фоні через Broadcaster.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
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
from bot.services.game import GameService
from bot.services.results import render_header, render_review

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())

# Фонові розсилки: тримаємо посилання, щоб задачі не збирав GC.
background_tasks: set[asyncio.Task] = set()
# Текст `/broadcast`, що чекає підтвердження: admin_id → HTML. У пам'яті: після рестарту
# просто просимо надіслати команду ще раз.
pending_broadcasts: dict[int, str] = {}

SETCODE_MIN, SETCODE_MAX = 2, 64
CLOSE_IN_MIN, CLOSE_IN_MAX = 1, 600


def _spawn(coro: Awaitable[None]) -> None:
    async def runner() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            log.exception("Фонова задача адміна впала")

    task = asyncio.create_task(runner())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


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
    session_factory: async_sessionmaker[AsyncSession],
    report: Report,
    *,
    force: bool = False,
    arg: int = 0,
    preview: str = "",
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
        texts.confirm(
            action,
            state=state,
            counters=counters,
            todo_count=todo_count,
            force=force,
            arg=arg,
            preview=preview,
        ),
        reply_markup=confirm_kb(action, force, arg),
    )


@router.message(Command("open_reg"))
async def cmd_open_reg(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "open_reg", session_factory, content_report)


@router.message(Command("close_reg"))
async def cmd_close_reg(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "close_reg", session_factory, content_report)


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
        bot, message.chat.id, "start_quest", session_factory, content_report, force=arg == "force"
    )


@router.message(Command("close_in"))
async def cmd_close_in(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    raw = (command.args or "").strip()
    if not raw.isdigit() or not CLOSE_IN_MIN <= int(raw) <= CLOSE_IN_MAX:
        await message.answer(texts.ADMIN_USAGE_CLOSE_IN)
        return
    await _ask_confirm(
        bot, message.chat.id, "close_in", session_factory, content_report, arg=int(raw)
    )


@router.message(Command("close_quest"))
async def cmd_close_quest(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "close_quest", session_factory, content_report)


@router.message(Command("publish_results"))
async def cmd_publish_results(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    await _ask_confirm(bot, message.chat.id, "publish_results", session_factory, content_report)


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
    content_report: Report,
) -> None:
    # `html_text` зберігає форматування, яке адмін зробив у Telegram (жирний, посилання),
    # а набрані вручну `<` `&` екранує: Telegram завжди прийме такий текст.
    parts = (message.html_text or "").split(None, 1)
    raw = parts[1].strip() if len(parts) == 2 else ""
    if not raw:
        await message.answer(texts.ADMIN_USAGE_BROADCAST)
        return
    pending_broadcasts[message.chat.id] = raw
    await _ask_confirm(
        bot, message.chat.id, "broadcast", session_factory, content_report, preview=raw
    )


@router.callback_query(
    AdminAct.filter(F.action.in_({"open_reg", "close_reg", "start_quest", "close_quest", "publish_results"}))
)
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


async def _broadcast_and_report(
    broadcaster: Broadcaster, admin_chat_id: int, recipients: list[int], text: str, markup=None
) -> None:
    result = await broadcaster.send(recipients, text, reply_markup=markup)
    await broadcaster.bot.send_message(admin_chat_id, texts.broadcast_done(result))


async def _notify_closed(
    game: GameService,
    broadcaster: Broadcaster,
    admin_chat_id: int,
    closed: list,
    items: list[tuple[int, str]],
) -> None:
    for attempt in closed:  # прибираємо кнопки в поточних питаннях (з розрахунком на ліміти)
        await broadcaster.acquire()
        await game.messenger.remove_keyboard(attempt.tg_id, attempt.q_message_id)
    result = await broadcaster.send_personal(items)
    await broadcaster.bot.send_message(admin_chat_id, texts.broadcast_done(result))


async def _publish_results(
    game: GameService,
    broadcaster: Broadcaster,
    session_factory: async_sessionmaker[AsyncSession],
    admin_chat_id: int,
) -> None:
    async with session_factory() as session:
        rows = await repo.leaderboard(session)
        ids = [r.attempt_id for r in rows]
        attempts = {a.id: a for a in await repo.list_attempts_by_ids(session, ids)}
        answers = await repo.answers_by_attempt(session, ids)

    items: list[tuple[int, str]] = []
    for row in rows:
        attempt = attempts[row.attempt_id]
        items.append(
            (
                row.tg_id,
                render_header(game.content, row.place, len(rows), attempt.score, len(attempt.plan)),
            )
        )
        items.extend((row.tg_id, chunk) for chunk in render_review(game.index, attempt, answers[attempt.id]))

    result = await broadcaster.send_personal(items)
    async with session_factory.begin() as session:
        await repo.mark_results_sent(session, ids)
    await broadcaster.bot.send_message(admin_chat_id, texts.broadcast_done(result))


@router.callback_query(Confirm.filter())
async def on_confirm(
    callback: CallbackQuery,
    callback_data: Confirm,
    session_factory: async_sessionmaker[AsyncSession],
    content: Content,
    content_report: Report,
    content_hash: str,
    broadcaster: Broadcaster,
    game: GameService,
) -> None:
    await callback.answer()
    admin_id = callback.from_user.id
    if not callback_data.yes:
        pending_broadcasts.pop(admin_id, None)
        await _finish_message(callback, texts.CANCELLED)
        return

    action = callback_data.action
    if action == "reset_all" and callback_data.arg == 0:
        # Подвійне підтвердження (DESIGN §10): перше «так» лише веде до другого.
        if isinstance(callback.message, Message):
            with contextlib.suppress(TelegramAPIError):
                await callback.message.edit_text(
                    texts.second_confirm("reset_all"), reply_markup=confirm_kb("reset_all", arg=1)
                )
        return

    now = datetime.now(UTC)
    followup: Awaitable[None] | None = None
    closed: list = []
    notified = 0
    try:
        async with session_factory.begin() as session:
            if action == "open_reg":
                await phases.open_registration(session)
            elif action == "close_reg":
                await phases.close_registration(session)
            elif action == "start_quest":
                await phases.start_quest(
                    session,
                    content_hash=content_hash,
                    now=now,
                    todo_count=len(content_report.todos),
                    force=callback_data.force,
                )
                recipients = await repo.list_registered_ids(session)
                notified = len(recipients)
                followup = _broadcast_and_report(
                    broadcaster,
                    admin_id,
                    recipients,
                    content.messages["quest_started"],
                    start_game_kb(content),
                )
            elif action == "close_in":
                await phases.set_closes_in(session, minutes=callback_data.arg, now=now)
                recipients = await repo.list_registered_ids(session)
                notified = len(recipients)
                followup = _broadcast_and_report(
                    broadcaster,
                    admin_id,
                    recipients,
                    content.messages["closing_soon"].format(minutes=callback_data.arg),
                )
            elif action == "close_quest":
                closed = await phases.close_quest(session, now=now)
                notified = len(closed)
            elif action == "publish_results":
                await phases.publish_results(session)
                notified = len(await repo.leaderboard(session))
                followup = _publish_results(game, broadcaster, session_factory, admin_id)
            elif action == "reset_all":
                await phases.reset_all(session)
                game.scheduler.cancel_all()
            elif action == "broadcast":
                text = pending_broadcasts.pop(admin_id, None)
                if text is None:
                    raise phases.PhaseConflict(texts.NOTHING_TO_BROADCAST)
                recipients = await repo.list_registered_ids(session)
                notified = len(recipients)
                followup = _broadcast_and_report(broadcaster, admin_id, recipients, text)
            else:
                return
    except phases.PhaseConflict as exc:
        await _finish_message(callback, texts.conflict(str(exc)))
        return

    if action == "close_quest":
        # Після коміту: `close_notifications` відкриває власні сесії, а в одній транзакції
        # з `close_quest` вони чекали б на блокування тих самих рядків.
        game.cancel_timers(closed)
        items = await game.close_notifications(closed)
        followup = _notify_closed(game, broadcaster, admin_id, closed, items)

    await _finish_message(callback, texts.done(action, notified))
    if followup is not None:
        # Розсилка в фоні: підтвердження не чекає десятки секунд на 200 учасників.
        _spawn(followup)
