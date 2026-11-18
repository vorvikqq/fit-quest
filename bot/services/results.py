"""Результати та розбір (DESIGN §6.8, FR-23): місце, бал, правильні відповіді з поясненнями."""

from __future__ import annotations

from collections.abc import Mapping
from html import escape

from bot.content.models import Content
from bot.db.models import Answer, Attempt
from bot.services.plan import ContentIndex

MESSAGE_LIMIT = 4096
CHUNK_LIMIT = 3800  # запас: рахуємо довжину HTML-джерела, а Telegram рахує розібраний текст


def render_header(content: Content, place: int, participants: int, score: int, total: int) -> str:
    return content.messages["results"].format(
        place=place, participants=participants, score=score, total=total
    )


def _correct_answer_text(index: ContentIndex, qid: str) -> str | None:
    info = index.info(qid)
    correct = info.question.correct
    if correct is None:
        return None
    if info.section.question_type == "binary":
        labels = info.section.labels or []
        return escape(labels[correct]) if correct < len(labels) else None
    options = info.question.options or []
    return escape(options[correct]) if correct < len(options) else None


def render_review(
    index: ContentIndex, attempt: Attempt, answers: Mapping[str, Answer]
) -> list[str]:
    """Розбір усіх питань у порядку проходження, розбитий на повідомлення ≤ ліміту."""
    messages = index.content.messages
    blocks: list[str] = []
    for number, entry in enumerate(attempt.plan, start=1):
        qid = entry["qid"]
        info = index.info(qid)
        answer = answers.get(qid)
        if answer is None:
            mark = "➖"  # спроба закрита до цього питання
        elif answer.chosen is None:
            mark = "⏰"
        else:
            mark = "✅" if answer.is_correct else "❌"

        lines = [f"<b>{number}.</b> {mark} {info.question.text}"]
        right = _correct_answer_text(index, qid)
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
