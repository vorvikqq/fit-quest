"""Тексти для організаторів (адмін-інтерфейс). HTML-розмітка.

Тексти для учасників тут не живуть: вони в `quest.yaml → messages`.
"""

from __future__ import annotations

from html import escape

from bot.content.textutil import plain
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
    "reset_all": "Скинути все?",
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
    elif action == "reset_all":
        text += (
            f"\nБуде видалено всіх учасників ({counters.registered}), їхні спроби, відповіді й відгуки, "
            "а етап повернеться в DRAFT. Кодове слово лишиться. Це незворотно."
        )
    elif action == "broadcast":
        text += f"\nОтримувачів: {counters.registered}.\n\n— — —\n{preview}\n— — —"
    return text


def second_confirm(action: str) -> str:
    return f"🧨 <b>Ще раз, остаточно:</b> {ACTION_TITLES[action]} Повернути назад не вийде."


def done(action: str, registered: int = 0) -> str:
    return {
        "open_reg": "✅ Реєстрацію відкрито.",
        "close_reg": "🔒 Реєстрацію закрито.",
        "start_quest": f"🚀 Квест стартував. Розсилаю повідомлення ({registered})…",
        "close_in": f"⏳ Час закриття призначено. Розсилаю попередження ({registered})…",
        "close_quest": f"🔒 Квест закрито. Розсилаю повідомлення тим, хто не встиг ({registered})…",
        "publish_results": f"📊 Результати опубліковано. Розсилаю ({registered})…",
        "broadcast": f"📣 Розсилаю ({registered})…",
        "reset_all": "🧹 Скинуто: учасників і спроб більше немає, етап DRAFT.",
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


def format_ms(ms: int) -> str:
    seconds = ms / 1000
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def stats(stats, content, total_questions: int) -> str:
    """`stats` — `services.stats.Stats`."""
    messages = content.messages
    lines = [
        "<b>Статистика</b> (без адмінів)",
        f"Зареєстровано: <b>{stats.registered}</b>",
        f"Грають: {stats.in_progress} · Завершили: {stats.finished} · "
        f"Закрито недопройдених: {stats.closed_unfinished}",
    ]
    if stats.avg_score is not None:
        lines.append(f"Середній бал (завершили): {stats.avg_score:.1f} з {total_questions}")
    if stats.by_status:
        lines.append("\n<b>За статусом</b>")
        for key, n in sorted(stats.by_status.items(), key=lambda kv: -kv[1]):
            label = messages.get(f"status_{key.lower()}", key)
            lines.append(f"• {escape(label)}: {n}")
    if stats.by_course:
        lines.append("\n<b>Студенти ФІТ за курсом</b>")
        for key, n in sorted(stats.by_course.items()):
            lines.append(f"• {escape(messages.get(f'course_{key.lower()}', key))}: {n}")
    if stats.hardest:
        lines.append("\n<b>Найскладніші питання</b>")
        index = {q.id: q for _, _, q in content.iter_questions()}
        for i, h in enumerate(stats.hardest, start=1):
            q = index.get(h.qid)
            snippet = escape(plain(q.text).replace("\n", " ")[:70]) if q else h.qid
            lines.append(
                f"{i}. <code>{h.qid}</code> «{snippet}» — {h.percent}% ({h.correct} з {h.answered})"
            )
    return "\n".join(lines)


def top(rows, total: int, limit: int) -> str:
    if not rows:
        return "Рейтинг порожній: ще ніхто не завершив квест."
    lines = [f"<b>Топ-{limit}</b> (бал, сумарний час)"]
    for r in rows:
        name = escape(r.full_name)
        link = f'<a href="tg://user?id={r.tg_id}">{name}</a>'
        handle = f" @{escape(r.username)}" if r.username else ""
        mark = "" if r.status == "FINISHED" else " ⏹"  # недопройшов: закрили на питанні
        lines.append(
            f"{r.place}. {link}{handle} — {r.score}/{total} · {format_ms(r.total_time_ms)}{mark}"
        )
    return "\n".join(lines)


def user_card(user, attempt, answers, total: int, content) -> str:
    messages = content.messages
    name = escape(user.full_name or user.tg_name or str(user.tg_id))
    handle = f" @{escape(user.username)}" if user.username else ""
    lines = [
        f'<b>{name}</b>{handle} · <a href="tg://user?id={user.tg_id}">профіль</a> · '
        f"id <code>{user.tg_id}</code>"
    ]
    if user.registered_at is None:
        lines.append("Не зареєстрований")
    else:
        status = messages.get(f"status_{(user.status or '').lower()}", user.status)
        course = messages.get(f"course_{(user.course or '').lower()}", "")
        lines.append(f"Статус: {escape(str(status))}{' · ' + escape(course) if course else ''}")
        lines.append(f"Зареєстрований: {user.registered_at:%d.%m %H:%M}")
    flags = [
        label
        for label, on in (
            ("адмін", user.is_admin),
            ("виключений з рейтингу", user.excluded),
            ("заблокував бота", user.blocked_bot),
        )
        if on
    ]
    if flags:
        lines.append("Позначки: " + ", ".join(flags))
    if attempt is None:
        lines.append("Спроби немає")
        return "\n".join(lines)
    lines.append(
        f"\nСпроба: <b>{attempt.status}</b> · відповідей {attempt.cursor} з {total} · "
        f"бал {attempt.score} · час {format_ms(attempt.total_time_ms)}"
    )
    if attempt.feedback:
        lines.append(f"Відгук: «{escape(attempt.feedback)}»")
    marks = []
    for a in answers:
        mark = "⏰" if a.chosen is None else ("✅" if a.is_correct else "❌")
        marks.append(f"{a.qid} {mark} {a.response_ms / 1000:.1f} с")
    if marks:
        lines.append("\n" + "\n".join(marks))
    return "\n".join(lines)


def chunk_pre(text: str, limit: int = 3500) -> list[str]:
    """Розбиває довгий звіт на шматки для `<pre>`, не ламаючи рядки."""
    chunks, current = [], ""
    for line in text.splitlines():
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return [f"<pre>{escape(c)}</pre>" for c in chunks] or ["<pre></pre>"]
