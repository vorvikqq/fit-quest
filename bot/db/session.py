"""Підключення до БД."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: після коміту об'єкти лишаються читабельними
    # (Telegram-виклики йдуть уже після транзакції, DESIGN §6.4).
    return async_sessionmaker(engine, expire_on_commit=False)
