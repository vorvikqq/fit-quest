"""Тексти для організаторів (адмін-інтерфейс). HTML-розмітка.

Тексти для учасників тут не живуть: вони в `quest.yaml → messages`.
"""

from __future__ import annotations

from html import escape

from bot.db.repo import Counters
from bot.db.models import GameState
from bot.services.broadcast import BroadcastResult

ACTION_TITLES = {
    "open_reg": "Відкрити реєстрацію?",
    "close_reg": "Закрити реєстрацію?",
    "start_quest": "Стартувати квест?",
}

ADMIN_USAGE_SETCODE = "Використання: <code>/setcode слово</code>"
ADMIN_USAGE_START_QUEST = "Використання: <code>/start_quest</code> або <code>/start_quest force</code>"
CANCELLED = "Скасовано."


def panel(
    state: GameState, counters: Counters, question_count: int, todo_count: int
) -> str:
    code = f"<code>{escape(state.access_code)}</code>" if state.access_code.strip() else "⚠️ не задано (/setcode)"
    reg = "відкрита ✅" if state.registration_open else "закрита 🔒"
    todo = f"⚠️ TODO: {todo_count}" if todo_count else "TODO немає ✅"
    return (
        "<b>Панель адміна</b>\n"
        f"Етап: <b>{state.phase}</b>\n"
        f"Реєстрація: {reg}\n"
        f"Кодове слово: {code}\n"
        "\n"
        f"Зареєстровано: <b>{counters.registered}</b>\n"
        f"Грають: {counters.in_progress} · Завершили: {counters.finished} · "
        f"Закрито недопройдених: {counters.closed_unfinished}\n"
        "\n"
        f"Контент: {question_count} питань, {todo}"
    )


def confirm(action: str, registered: int, todo_count: int, force: bool, code: str) -> str:
    text = f"<b>{ACTION_TITLES[action]}</b>"
    if action == "open_reg":
        text += f"\nКодове слово: <code>{escape(code)}</code>" if code.strip() else "\n⚠️ Кодове слово не задано, ніхто не зможе зареєструватись (/setcode)."
    if action == "start_quest":
        text += f"\nЗареєстровано: {registered}. Усім їм піде повідомлення «Квест стартував»."
        if todo_count:
            text += f"\n⚠️ Примусовий старт: у контенті {todo_count} незаповнених питань."
    return text


def done(action: str, registered: int) -> str:
    return {
        "open_reg": "✅ Реєстрацію відкрито.",
        "close_reg": "🔒 Реєстрацію закрито.",
        "start_quest": f"🚀 Квест стартував. Розсилаю повідомлення ({registered})…",
    }[action]


def code_changed(code: str) -> str:
    return f"✅ Кодове слово змінено: <code>{escape(code)}</code>"


def conflict(reason: str) -> str:
    return f"⚠️ {escape(reason)}"


def broadcast_done(result: BroadcastResult) -> str:
    text = f"📣 Розсилку завершено: надіслано {result.sent} з {result.total}."
    if result.blocked:
        text += f" Заблокували бота: {result.blocked}."
    if result.failed:
        text += f" Помилок: {result.failed} (див. логи)."
    return text
