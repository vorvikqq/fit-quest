"""Клавіатури й фабрики callback-даних."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.content.models import Content
from bot.db.models import COURSES, UserStatus

START_GAME_DATA = "game:start"  # кнопка «Почати квест»
FEEDBACK_SKIP_DATA = "fb:skip"


class GoCb(CallbackData, prefix="go"):
    """Кнопка «Почати раунд» на картці (DESIGN §6.2)."""

    cursor: int


class AnsCb(CallbackData, prefix="a"):
    """Відповідь на питання: `a:{cursor}:{btn}` (DESIGN §6.4)."""

    cursor: int
    btn: int


class StatusCb(CallbackData, prefix="st"):
    value: str


class CourseCb(CallbackData, prefix="co"):
    value: str


class AdminAct(CallbackData, prefix="act"):
    action: str


class Confirm(CallbackData, prefix="cf"):
    action: str
    force: bool = False
    arg: int = 0  # напр., хвилини для /close_in
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


def card_kb(content: Content, cursor: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=content.messages["btn_start_round"], callback_data=GoCb(cursor=cursor))
    return kb.as_markup()


def answer_kb(buttons: tuple[tuple[str, int], ...], cursor: int) -> InlineKeyboardMarkup:
    """Усі кнопки в один ряд: для choice це лише літери, для binary — короткі мітки."""
    kb = InlineKeyboardBuilder()
    for label, btn in buttons:
        kb.button(text=label, callback_data=AnsCb(cursor=cursor, btn=btn))
    return kb.adjust(len(buttons)).as_markup()


def skip_feedback_kb(content: Content) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=content.messages["btn_skip"], callback_data=FEEDBACK_SKIP_DATA)
    return kb.as_markup()


def admin_panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔓 Відкрити реєстрацію", callback_data=AdminAct(action="open_reg"))
    kb.button(text="🔒 Закрити реєстрацію", callback_data=AdminAct(action="close_reg"))
    kb.button(text="🚀 Старт квесту", callback_data=AdminAct(action="start_quest"))
    kb.button(text="🔄 Оновити", callback_data=AdminAct(action="refresh"))
    return kb.adjust(2).as_markup()


def confirm_kb(action: str, force: bool = False, arg: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так", callback_data=Confirm(action=action, force=force, arg=arg, yes=True))
    kb.button(text="❌ Ні", callback_data=Confirm(action=action, force=force, arg=arg, yes=False))
    return kb.adjust(2).as_markup()
