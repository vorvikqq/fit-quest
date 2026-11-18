"""Підключення до БД."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


# 200 учасників тиснуть кнопки одночасно, і кожне натискання робить кілька коротких запитів.
# Пул більший за стандартний (5+10) як запас на сплески. Вимір (docs/LOAD_TEST.md) показав,
# що пропускну здатність він майже не змінює: межа в CPU/БД, а не в пулі.
# Загалом 40 з'єднань, а у Postgres за замовчуванням max_connections = 100.
POOL_SIZE = 20
MAX_OVERFLOW = 20


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        database_url, pool_pre_ping=True, pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: після коміту об'єкти лишаються читабельними
    # (Telegram-виклики йдуть уже після транзакції, DESIGN §6.4).
    return async_sessionmaker(engine, expire_on_commit=False)
