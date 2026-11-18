"""Фікстури для тестів з PostgreSQL.

БД беремо з `TEST_DATABASE_URL` (за замовчуванням — тимчасовий контейнер на
127.0.0.1:55432, див. README у docker-compose). Якщо БД недоступна, DB-тести
пропускаються, а решта працює.

УВАГА: схема `public` у цій БД на початку сесії **видаляється**. Не вказуйте
сюди робочу базу.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from bot.config import PROJECT_ROOT
from bot.db.models import Base

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://quest:quest@127.0.0.1:55432/quest"
)


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


def alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "bot" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    return cfg


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Чиста БД з накоченою міграцією (так само, як у проді)."""
    try:
        asyncio.run(_reset_schema())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL недоступний ({TEST_DB_URL}): {exc}")
    command.upgrade(alembic_config(), "head")
    return TEST_DB_URL


@pytest_asyncio.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, pool_size=20, max_overflow=20)
    async with eng.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
