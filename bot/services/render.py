"""Формування повідомлень гри: картки раундів і питання (DESIGN §6.2, §6.3).

Чисті функції без Telegram, тож їх легко тестувати.
Текст питання вважається HTML (може містити `<b>`); варіанти відповіді екрануються,
бо містять `<<`, `#include <cmath>` тощо.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from bot.content.textutil import plain, tg_len
from bot.services.plan import ContentIndex, Plan

CAPTION_LIMIT = 1024  # ліміт підпису до фото в Telegram
# Після відповіді до підпису дописується «✅ Відповідь прийнято»: лишаємо для нього місце,
# інакше правка підпису впаде й учасник не побачить підтвердження.
SUFFIX_RESERVE = 30
LETTERS = "ABCD"


@dataclass(frozen=True)
class QuestionView:
    text: str  # HTML: шапка + питання (+ варіанти)
    image: str | None  # шлях відносно content/
    split: bool  # True: фото окремим повідомленням, питання з кнопками наступним
    buttons: tuple[tuple[str, int], ...]  # (підпис, індекс кнопки)

    @property
    def caption_mode(self) -> bool:
        """Питання живе в підписі до фото (редагувати треба `caption`, а не `text`)."""
        return self.image is not None and not self.split


def render_question(index: ContentIndex, plan: Plan, cursor: int) -> QuestionView:
    entry = plan[cursor]
    info = index.info(entry["qid"])
    messages = index.content.messages
    number, total = index.position_in_round(plan, cursor)

    header = messages["question_header"].format(
        round=info.round_no,
        rounds=index.rounds_total,
        number=number,
        total=total,
        seconds=info.settings.time_limit,
    )
    parts = [header, info.question.text]

    if info.section.question_type == "choice":
        options = info.question.options or []
        order = entry["option_order"]
        parts.append(
            "\n".join(f"<b>{LETTERS[pos]})</b> {escape(options[orig])}" for pos, orig in enumerate(order))
        )
        buttons = tuple((LETTERS[pos], pos) for pos in range(len(order)))
    else:
        labels = info.section.labels or []
        buttons = tuple((label, i) for i, label in enumerate(labels))

    text = "\n\n".join(parts)
    image = info.question.image
    split = image is not None and tg_len(plain(text)) > CAPTION_LIMIT - SUFFIX_RESERVE
    return QuestionView(text=text, image=image, split=split, buttons=buttons)


def render_card(index: ContentIndex, plan: Plan, cursor: int) -> str:
    """Картка раунду/секції. На початку раунду: назва + вступ раунду + вступ першої секції."""
    info = index.info(plan[cursor]["qid"])
    messages = index.content.messages
    lines: list[str] = []
    if index.boundary_at(plan, cursor) == "round":
        lines.append(f"<b>{info.round.title}</b>")
        if info.round.intro:
            lines.append(info.round.intro)
    if info.section.intro:
        lines.append(info.section.intro)
    lines.append(
        messages["round_stats"].format(
            count=len(info.section.questions), seconds=info.settings.time_limit
        )
    )
    return "\n\n".join(lines)
