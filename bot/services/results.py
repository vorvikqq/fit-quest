"""Результати та розбір (DESIGN §6.8, FR-23): місце, бал, відповіді з поясненнями."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from bot.content.models import Content
from bot.db.models import Answer, Attempt
from bot.services.plan import ContentIndex

MESSAGE_LIMIT = 4096
CHUNK_LIMIT = 3800  # запас: рахуємо довжину HTML-джерела, а Telegram рахує розібраний текст


def render_header(
    content: Content, place: int | None, participants: int, score: int, total: int
) -> str:
    """Заголовок результатів. Без місця (адмін чи виключений з рейтингу) показуємо лише бал."""
    if place is None:
        return content.messages["results_no_place"].format(score=score, total=total)
    return content.messages["results"].format(
        place=place, participants=participants, score=score, total=total
    )


def _option_text(index: ContentIndex, qid: str, option: int | None) -> str | None:
    """Текст варіанта за індексом в ОРИГІНАЛЬНОМУ порядку з yaml (так він збережений у `answers`)."""
    if option is None:
        return None
    info = index.info(qid)
    if info.section.question_type == "binary":
        labels = info.section.labels or []
        return escape(labels[option]) if 0 <= option < len(labels) else None
    options = info.question.options or []
    return escape(options[option]) if 0 <= option < len(options) else None


def render_review(
    index: ContentIndex, attempt: Attempt, answers: Mapping[str, Answer]
) -> list[str]:
    """Розбір усіх питань у порядку проходження, розбитий на повідомлення ≤ ліміту.

    Для кожного питання: позначка (✅ правильно, ❌ хибно, ⏰ не встиг, ➖ не дійшов), для
    помилок «Твоя відповідь» і «Правильна відповідь», а також пояснення.
    """
    messages = index.content.messages
    blocks: list[str] = []
    for number, entry in enumerate(attempt.plan, start=1):
        qid = entry["qid"]
        info = index.info(qid)
        answer = answers.get(qid)
        lines: list[str]

        if answer is None:
            mark, correct = "➖", False  # спроба закрита до цього питання
        elif answer.chosen is None:
            mark, correct = "⏰", False
        else:
            correct = answer.is_correct
            mark = "✅" if correct else "❌"
        lines = [f"<b>{number}.</b> {mark} {info.question.text}"]

        if answer is not None and answer.chosen is None:
            lines.append(messages["review_timeout"])
        elif answer is not None and not correct:
            chosen = _option_text(index, qid, answer.chosen)
            if chosen is not None:
                lines.append(messages["review_your_answer"].format(answer=chosen))
        if not correct:  # правильна відповідь потрібна тим, хто помилився чи не встиг
            right = _option_text(index, qid, info.question.correct)
            if right is not None:
                lines.append(messages["review_right_answer"].format(answer=right))
        if info.question.explanation.strip():
            lines.append(messages["review_explanation"].format(text=escape(info.question.explanation)))
        blocks.append("\n".join(lines))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > CHUNK_LIMIT:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
