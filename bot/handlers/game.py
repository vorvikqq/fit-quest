"""ТИМЧАСОВА заглушка кнопки «Почати квест». Замінюється ігровим ядром (DESIGN §6)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.content.models import Content
from bot.keyboards import START_GAME_DATA

router = Router(name="game")


@router.callback_query(F.data == START_GAME_DATA)
async def start_game_stub(callback: CallbackQuery, content: Content) -> None:
    await callback.answer(content.messages["game_not_ready"], show_alert=True)
