"""Адмін-інструменти (DESIGN §10): /stats /top /export /user /exclude /include /validate
/reload_content /warmup /reset_me /reset_all. Доступ лише для ADMIN_IDS."""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot import texts
from bot.content.loader import ContentError, load_and_validate
from bot.db import repo
from bot.handlers.admin import _ask_confirm
from bot.middlewares import AdminOnlyMiddleware
from bot.services.content_reload import ReloadRefused, load_for_reload
from bot.services.export import build_exports
from bot.services.game import GameService
from bot.services.media import MediaService
from bot.services.stats import collect_stats

log = logging.getLogger(__name__)

router = Router(name="admin_tools")
router.message.filter(F.chat.type == "private")
router.message.middleware(AdminOnlyMiddleware())

TOP_DEFAULT, TOP_MAX = 10, 100
NOT_FOUND = "Користувача не знайдено. Вкажи tg_id або @username (його видно після першого /start)."

SessionFactory = async_sessionmaker[AsyncSession]


# --- /stats, /top ----------------------------------------------------------


@router.message(Command("stats"))
async def cmd_stats(message: Message, session_factory: SessionFactory, game: GameService) -> None:
    async with session_factory() as session:
        stats = await collect_stats(session)
    await message.answer(texts.stats(stats, game.content, game.content.question_count))


@router.message(Command("top"))
async def cmd_top(
    message: Message, command: CommandObject, session_factory: SessionFactory, game: GameService
) -> None:
    raw = (command.args or "").strip()
    if raw and not (raw.isdigit() and 1 <= int(raw) <= TOP_MAX):
        await message.answer(f"Використання: <code>/top</code> або <code>/top N</code> (1–{TOP_MAX})")
        return
    limit = int(raw) if raw else TOP_DEFAULT
    async with session_factory() as session:
        rows = await repo.leaderboard(session, limit=limit)
    await message.answer(texts.top(rows, game.content.question_count, limit))


# --- /board ----------------------------------------------------------------


@router.message(Command("board"))
async def cmd_board(message: Message, board_url: str, board_token: str) -> None:
    if not board_token:
        await message.answer("Табло вимкнено: задай BOARD_TOKEN у .env і перезапусти бота.")
        return
    await message.answer(
        f"📺 Живе табло для Zoom:\n{board_url}/board?token={board_token}\n\n"
        "Посилання містить секретний токен: не публікуй його в чаті Zoom.",
        disable_web_page_preview=True,
    )


# --- /export ---------------------------------------------------------------


@router.message(Command("export"))
async def cmd_export(message: Message, session_factory: SessionFactory, game: GameService) -> None:
    async with session_factory() as session:
        files = await build_exports(session, game.index)
    for name, data in files.items():
        await message.answer_document(BufferedInputFile(data, filename=name))


# --- /user, /exclude, /include ---------------------------------------------


@router.message(Command("user"))
async def cmd_user(
    message: Message, command: CommandObject, session_factory: SessionFactory, game: GameService
) -> None:
    if not command.args:
        await message.answer("Використання: <code>/user tg_id|@username</code>")
        return
    async with session_factory() as session:
        user = await repo.find_user(session, command.args)
        if user is None:
            await message.answer(NOT_FOUND)
            return
        attempt = await repo.get_attempt_for_user(session, user.tg_id)
        answers = list(await repo.list_answers(session, attempt.id)) if attempt else []
    await message.answer(
        texts.user_card(user, attempt, answers, game.content.question_count, game.content),
        disable_web_page_preview=True,
    )


async def _set_excluded(
    message: Message, command: CommandObject, session_factory: SessionFactory, excluded: bool
) -> None:
    if not command.args:
        word = "exclude" if excluded else "include"
        await message.answer(f"Використання: <code>/{word} tg_id|@username</code>")
        return
    async with session_factory.begin() as session:
        user = await repo.find_user(session, command.args)
        if user is None:
            await message.answer(NOT_FOUND)
            return
        await repo.set_user_flag(session, user.tg_id, excluded=excluded)
    name = user.full_name or user.tg_name
    await message.answer(
        f"🚫 {name} виключено з рейтингу." if excluded else f"✅ {name} повернено в рейтинг."
    )


@router.message(Command("exclude"))
async def cmd_exclude(
    message: Message, command: CommandObject, session_factory: SessionFactory
) -> None:
    await _set_excluded(message, command, session_factory, True)


@router.message(Command("include"))
async def cmd_include(
    message: Message, command: CommandObject, session_factory: SessionFactory
) -> None:
    await _set_excluded(message, command, session_factory, False)


# --- /validate, /reload_content --------------------------------------------


@router.message(Command("validate"))
async def cmd_validate(message: Message, content_path: Path) -> None:
    """Перевіряє файл на диску просто зараз (а не знімок зі старту)."""
    try:
        content, report = load_and_validate(content_path)
    except (ContentError, OSError) as exc:
        await message.answer(texts.conflict(f"Файл не прочитано: {exc}"))
        return
    head = f"Раундів: {len(content.rounds)}, питань: {content.question_count}\n"
    if report.errors:
        head += f"❌ Помилок: {len(report.errors)}. Старт неможливий.\n"
    elif report.has_todo:
        head += f"⚠️ Незаповнених (TODO): {len(report.todos)}. Старт заблоковано без force.\n"
    else:
        head += "✅ Контент готовий до старту.\n"
    for chunk in texts.chunk_pre(head + "\n" + report.render()):
        await message.answer(chunk)


@router.message(Command("reload_content"))
async def cmd_reload(
    message: Message,
    content_path: Path,
    session_factory: SessionFactory,
    game: GameService,
    dispatcher: Dispatcher,
) -> None:
    try:
        loaded = await load_for_reload(content_path, session_factory)
    except ReloadRefused as exc:
        await message.answer(texts.conflict(str(exc)))
        return
    game.set_content(loaded.content)
    dispatcher["content"] = loaded.content
    dispatcher["content_report"] = loaded.report
    dispatcher["content_hash"] = loaded.content_hash
    note = "Контент перечитано" if loaded.changed else "Файл не змінився, контент перечитано"
    todo = f" TODO: {len(loaded.report.todos)}." if loaded.report.has_todo else ""
    await message.answer(f"✅ {note}: {loaded.content.question_count} питань.{todo}")


# --- /warmup ---------------------------------------------------------------


@router.message(Command("warmup"))
async def cmd_warmup(message: Message, bot: Bot, media: MediaService, game: GameService) -> None:
    result = await media.warmup(bot, message.chat.id, game.content)
    text = f"🔥 Кеш картинок: завантажено {len(result.uploaded)}, уже було {len(result.cached)}."
    if result.missing:
        text += "\n⚠️ Немає файлів: " + ", ".join(result.missing)
    await message.answer(text)


# --- /reset_me, /reset_all -------------------------------------------------


@router.message(Command("reset_me"))
async def cmd_reset_me(
    message: Message, session_factory: SessionFactory, game: GameService
) -> None:
    """Видаляє власну спробу адміна, щоб пройти квест знову (FR-31)."""
    tg_id = message.from_user.id  # type: ignore[union-attr]
    async with session_factory.begin() as session:
        attempt = await repo.get_attempt_for_user(session, tg_id)
        deleted = attempt is not None and await repo.delete_attempt_for_user(session, tg_id)
    if deleted:
        game.scheduler.cancel(attempt.id)
        await message.answer("🧹 Твою спробу видалено. Натисни /start, щоб пройти знову.")
    else:
        await message.answer("У тебе немає спроби.")


@router.message(Command("reset_all"))
async def cmd_reset_all(
    message: Message, bot: Bot, session_factory: SessionFactory, content_report
) -> None:
    await _ask_confirm(bot, message.chat.id, "reset_all", session_factory, content_report)
