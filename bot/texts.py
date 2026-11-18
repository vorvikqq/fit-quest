"""Тексти для організаторів (адмін-інтерфейс). HTML-розмітка.

Тексти для учасників тут не живуть: вони в `quest.yaml → messages`.
"""

from __future__ import annotations

from html import escape

from bot.db.models import GameState
from bot.db.repo import Counters
from bot.services.broadcast import BroadcastResult

ACTION_TITLES = {
    "open_reg": "Відкрити реєстрацію?",
    "close_reg": "Закрити реєстрацію?",
    "start_quest": "Стартувати квест?",
    "close_in": "Призначити закриття квесту?",
    "close_quest": "Закрити квест?",
    "publish_results": "Опублікувати результати?",
    "broadcast": "Надіслати розсилку?",
}

ADMIN_USAGE_SETCODE = "Використання: <code>/setcode слово</code>"
ADMIN_USAGE_START_QUEST = "Використання: <code>/start_quest</code> або <code>/start_quest force</code>"
ADMIN_USAGE_CLOSE_IN = "Використання: <code>/close_in хвилини</code> (від 1 до 600)"
ADMIN_USAGE_BROADCAST = "Використання: <code>/broadcast текст</code> (HTML-розмітка Telegram дозволена)"
CANCELLED = "Скасовано."
NOTHING_TO_BROADCAST = "Немає тексту для розсилки (можливо, бот перезапускався). Надішли /broadcast ще раз."


def panel(state: GameState, counters: Counters, question_count: int, todo_count: int) -> str:
    code = (
        f"<code>{escape(state.access_code)}</code>"
        if state.access_code.strip()
        else "⚠️ не задано (/setcode)"
    )
    reg = "відкрита ✅" if state.registration_open else "закрита 🔒"
    todo = f"⚠️ TODO: {todo_count}" if todo_count else "TODO немає ✅"
    closes = f"\nЗакриття заплановано: {state.closes_at:%H:%M}" if state.closes_at else ""
    return (
        "<b>Панель адміна</b>\n"
        f"Етап: <b>{state.phase}</b>\n"
        f"Реєстрація: {reg}\n"
        f"Кодове слово: {code}{closes}\n"
        "\n"
        f"Зареєстровано: <b>{counters.registered}</b>\n"
        f"Грають: {counters.in_progress} · Завершили: {counters.finished} · "
        f"Закрито недопройдених: {counters.closed_unfinished}\n"
        "\n"
        f"Контент: {question_count} питань, {todo}"
    )


def confirm(
    action: str,
    *,
    state: GameState,
    counters: Counters,
    todo_count: int = 0,
    force: bool = False,
    arg: int = 0,
    preview: str = "",
) -> str:
    text = f"<b>{ACTION_TITLES[action]}</b>"
    if action == "open_reg":
        if state.access_code.strip():
            text += f"\nКодове слово: <code>{escape(state.access_code)}</code>"
        else:
            text += "\n⚠️ Кодове слово не задано, ніхто не зможе зареєструватись (/setcode)."
    elif action == "start_quest":
        text += f"\nЗареєстровано: {counters.registered}. Усім їм піде повідомлення «Квест стартував»."
        if todo_count:
            text += f"\n⚠️ Примусовий старт: у контенті {todo_count} незаповнених питань."
    elif action == "close_in":
        text += (
            f"\nЧерез {arg} хв. Усім зареєстрованим ({counters.registered}) піде «Квест закриється». "
            "Автоматично квест не закриється: це зробить /close_quest."
        )
    elif action == "close_quest":
        text += (
            f"\nЗараз грають: {counters.in_progress}. Їм прийде «Квест закрито», промокод і їхні "
            "результати потраплять у рейтинг. Скасувати закриття не можна."
        )
    elif action == "publish_results":
        text += (
            f"\nКожен учасник з рейтингу отримає своє місце, бал і розбір усіх питань "
            f"(завершили: {counters.finished}, недопройшли: {counters.closed_unfinished})."
        )
    elif action == "broadcast":
        text += f"\nОтримувачів: {counters.registered}.\n\n— — —\n{preview}\n— — —"
    return text


def done(action: str, registered: int = 0) -> str:
    return {
        "open_reg": "✅ Реєстрацію відкрито.",
        "close_reg": "🔒 Реєстрацію закрито.",
        "start_quest": f"🚀 Квест стартував. Розсилаю повідомлення ({registered})…",
        "close_in": f"⏳ Час закриття призначено. Розсилаю попередження ({registered})…",
        "close_quest": f"🔒 Квест закрито. Розсилаю повідомлення тим, хто не встиг ({registered})…",
        "publish_results": f"📊 Результати опубліковано. Розсилаю ({registered})…",
        "broadcast": f"📣 Розсилаю ({registered})…",
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
