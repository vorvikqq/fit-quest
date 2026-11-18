"""Клавіатури й фабрики callback-даних."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.content.models import Content
from bot.db.models import COURSES, UserStatus

START_GAME_DATA = "go:start"


class StatusCb(CallbackData, prefix="st"):
    value: str


class CourseCb(CallbackData, prefix="co"):
    value: str


class AdminAct(CallbackData, prefix="act"):
    action: str


class Confirm(CallbackData, prefix="cf"):
    action: str
    force: bool = False
    yes: bool


def status_kb(content: Content) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for status in UserStatus:
        kb.button(
            text=content.messages[f"status_{status.value.lower()}"],
            callback_data=StatusCb(value=status.value),
        )
    return kb.adjust(1).as_markup()


def course_kb(content: Content) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for course in COURSES:
        kb.button(
            text=content.messages[f"course_{course.lower()}"], callback_data=CourseCb(value=course)
        )
    return kb.adjust(2).as_markup()


def start_game_kb(content: Content) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=content.messages["btn_start_quest"], callback_data=START_GAME_DATA)
    return kb.as_markup()


def admin_panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔓 Відкрити реєстрацію", callback_data=AdminAct(action="open_reg"))
    kb.button(text="🔒 Закрити реєстрацію", callback_data=AdminAct(action="close_reg"))
    kb.button(text="🚀 Старт квесту", callback_data=AdminAct(action="start_quest"))
    kb.button(text="🔄 Оновити", callback_data=AdminAct(action="refresh"))
    return kb.adjust(2).as_markup()


def confirm_kb(action: str, force: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так", callback_data=Confirm(action=action, force=force, yes=True))
    kb.button(text="❌ Ні", callback_data=Confirm(action=action, force=force, yes=False))
    return kb.adjust(2).as_markup()
