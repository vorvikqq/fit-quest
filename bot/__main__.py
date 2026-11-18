"""Точка входу: конфіг → контент → БД → long polling."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from bot.app import build_dispatcher
from bot.config import Settings, get_settings
from bot.content.loader import ContentError, file_sha256, load_and_validate
from bot.db import repo
from bot.db.session import make_engine, make_session_factory
from bot.services.broadcast import Broadcaster
from bot.services.game import GameService
from bot.services.media import MediaService
from bot.services.messenger import TelegramMessenger
from bot.services.timers import recover, run_watchdog

log = logging.getLogger("bot")


def load_content_or_exit(settings: Settings):
    """Валідація при старті (FR-30). Помилки зупиняють бота, TODO лише логуються."""
    try:
        content, report = load_and_validate(settings.content_path)
    except ContentError as exc:
        log.error("Контент не завантажено: %s", exc)
        sys.exit(1)

    for issue in report.errors:
        log.error("Контент: %s: %s", issue.where, issue.message)
    if report.errors:
        sys.exit(1)
    for issue in report.warnings:
        log.warning("Контент: %s: %s", issue.where, issue.message)
    if report.has_todo:
        log.warning(
            "У контенті %d незаповнених питань (TODO): квест не стартує без /start_quest force. "
            "Деталі: python -m bot.content.loader",
            len(report.todos),
        )
    log.info("Контент: %d раундів, %d питань", len(content.rounds), content.question_count)
    return content, report


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not settings.bot_token:
        log.error("BOT_TOKEN порожній: заповни .env")
        sys.exit(1)

    content, content_report = load_content_or_exit(settings)

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    async with session_factory.begin() as session:
        state = await repo.ensure_game_state(session, settings.access_code)
    log.info("Етап квесту: %s", state.phase)
    if not state.access_code.strip():
        log.warning("Кодове слово не задано: ніхто не зможе зареєструватись (/setcode або ACCESS_CODE)")
    if not settings.admin_ids:
        log.warning("ADMIN_IDS порожній: адмін-команди недоступні нікому")

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    media = MediaService(session_factory, settings.content_root)
    game = GameService(
        session_factory, content, TelegramMessenger(bot, content, media, session_factory)
    )
    dp = build_dispatcher(
        content=content,
        content_report=content_report,
        content_hash=file_sha256(settings.content_path),
        session_factory=session_factory,
        admin_ids=settings.admin_ids,
        broadcaster=Broadcaster(bot, session_factory),
        game=game,
    )

    try:
        me = await bot.get_me()
        log.info("Бот запущено: @%s", me.username)
        await recover(game)  # §7: прострочені питання закрити, живі — перепланувати
        watchdog = asyncio.create_task(run_watchdog(game))
        try:
            await dp.start_polling(bot)
        finally:
            watchdog.cancel()
            game.scheduler.cancel_all()
    except TelegramUnauthorizedError:
        log.error("Telegram відхилив BOT_TOKEN: перевір значення в .env")
        sys.exit(1)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
