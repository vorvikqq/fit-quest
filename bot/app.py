"""Збірка Dispatcher: залежності, middleware, роутери."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.loader import Report
from bot.content.models import Content
from bot.handlers import admin, after_game, registration
from bot.handlers import game as game_handlers
from bot.middlewares import ThrottlingMiddleware, UserUpsertMiddleware
from bot.services.broadcast import Broadcaster
from bot.services.game import GameService
from bot.services.ratelimit import SlidingWindowLimiter

CODE_ATTEMPTS_PER_MINUTE = 5  # DESIGN §13


def build_dispatcher(
    *,
    content: Content,
    content_report: Report,
    content_hash: str,
    session_factory: async_sessionmaker[AsyncSession],
    admin_ids: list[int],
    broadcaster: Broadcaster,
    game: GameService,
    throttle_interval: float = 0.5,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Залежності, які aiogram підставляє в хендлери за іменем параметра.
    dp["content"] = content
    dp["content_report"] = content_report
    dp["content_hash"] = content_hash
    dp["session_factory"] = session_factory
    dp["admin_ids"] = admin_ids
    dp["broadcaster"] = broadcaster
    dp["game"] = game
    dp["code_limiter"] = SlidingWindowLimiter(CODE_ATTEMPTS_PER_MINUTE, 60)

    # Порядок: throttling (дешево, відсікає спам) → user-upsert → хендлери.
    dp.callback_query.outer_middleware(ThrottlingMiddleware(throttle_interval))
    upsert = UserUpsertMiddleware(session_factory, admin_ids)
    dp.message.outer_middleware(upsert)
    dp.callback_query.outer_middleware(upsert)

    # Порядок роутерів важливий: admin → game → after_game → registration (з fallback наприкінці).
    dp.include_router(admin.router)
    dp.include_router(game_handlers.router)
    dp.include_router(after_game.router)
    dp.include_router(registration.router)
    return dp
