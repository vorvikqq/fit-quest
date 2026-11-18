"""Реєстрація. Поки що лише заглушка /start (повна FSM — наступний крок)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo

router = Router(name="registration")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    content: Content,
    session_factory: async_sessionmaker[AsyncSession],
    admin_ids: list[int],
) -> None:
    user = message.from_user
    if user is None:  # повідомлення від каналу тощо
        return
    async with session_factory.begin() as session:
        await repo.upsert_user(
            session,
            tg_id=user.id,
            username=user.username,
            tg_name=user.full_name,
            is_admin=user.id in admin_ids,
        )
    await message.answer(content.messages["welcome"])
