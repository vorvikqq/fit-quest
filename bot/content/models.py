"""Pydantic-схема `content/quest.yaml` (DESIGN §5).

Моделі навмисно «м'які»: вони перевіряють лише форму даних (типи, невідомі
ключі). Змістові правила (кількість варіантів, `correct` у межах, ліміти
довжини, TODO тощо) перевіряє `loader.py`, щоб `/validate` міг показати
**усі** проблеми одразу, а не впасти на першій.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    # Невідомий ключ у yaml (друкарська помилка) — помилка, а не мовчазне ігнорування.
    model_config = ConfigDict(extra="forbid")


class Promo(_Strict):
    code: str
    text: str


class Defaults(_Strict):
    time_limit: int = 20
    shuffle_options: bool = True
    shuffle_questions: bool = False


class Meta(_Strict):
    title: str
    promo: Promo
    defaults: Defaults = Defaults()


class Question(_Strict):
    id: str
    text: str
    image: str | None = None  # шлях відносно content/
    options: list[str] | None = None  # лише для choice
    correct: int | None = None  # індекс варіанта (choice) або мітки (binary)
    explanation: str = ""
    todo: str | None = None  # непорожній рядок = «не готово»


class Section(_Strict):
    id: str
    intro: str | None = None
    question_type: Literal["choice", "binary"]
    labels: list[str] | None = None  # для binary
    # None = успадкувати від раунду, а далі від meta.defaults
    time_limit: int | None = None
    shuffle_options: bool | None = None
    shuffle_questions: bool | None = None
    questions: list[Question]


class Round(_Strict):
    id: str
    title: str
    intro: str | None = None
    time_limit: int | None = None
    shuffle_options: bool | None = None
    shuffle_questions: bool | None = None
    sections: list[Section]


@dataclass(frozen=True)
class SectionSettings:
    """Ефективні налаштування секції після успадкування."""

    time_limit: int
    shuffle_options: bool
    shuffle_questions: bool


class Content(_Strict):
    meta: Meta
    messages: dict[str, str]
    rounds: list[Round]

    def iter_questions(self) -> Iterator[tuple[Round, Section, Question]]:
        for rnd in self.rounds:
            for section in rnd.sections:
                for question in section.questions:
                    yield rnd, section, question

    @property
    def question_count(self) -> int:
        return sum(1 for _ in self.iter_questions())

    def settings_for(self, rnd: Round, section: Section) -> SectionSettings:
        """Секція → раунд → `meta.defaults`."""
        defaults = self.meta.defaults

        def pick[T](*values: T | None, fallback: T) -> T:
            return next((v for v in values if v is not None), fallback)

        return SectionSettings(
            time_limit=pick(section.time_limit, rnd.time_limit, fallback=defaults.time_limit),
            shuffle_options=pick(
                section.shuffle_options, rnd.shuffle_options, fallback=defaults.shuffle_options
            ),
            shuffle_questions=pick(
                section.shuffle_questions, rnd.shuffle_questions, fallback=defaults.shuffle_questions
            ),
        )
