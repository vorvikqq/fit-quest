"""Персональний план спроби й індекс контенту (DESIGN §6.1).

`plan` — список `{"qid": ..., "option_order": [...]}` у порядку проходження.
`option_order[i]` — індекс **оригінального** варіанта, який учасник бачить на позиції `i`.
Для binary порядок завжди `[0, 1]` (FR-10: мітки мають сталий порядок).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Literal

from bot.content.models import Content, Question, Round, Section, SectionSettings

PlanEntry = dict[str, Any]
Plan = list[PlanEntry]
Boundary = Literal["round", "section"]


@dataclass(frozen=True)
class QInfo:
    round: Round
    section: Section
    question: Question
    settings: SectionSettings
    round_no: int  # 1-based


class ContentIndex:
    """Швидкий пошук питання за `qid` та службова арифметика навколо плану."""

    def __init__(self, content: Content) -> None:
        self.content = content
        self.rounds_total = len(content.rounds)
        self._by_qid: dict[str, QInfo] = {}
        self._round_sizes: dict[str, int] = {}
        for round_no, rnd in enumerate(content.rounds, start=1):
            self._round_sizes[rnd.id] = sum(len(s.questions) for s in rnd.sections)
            for section in rnd.sections:
                settings = content.settings_for(rnd, section)
                for question in section.questions:
                    self._by_qid[question.id] = QInfo(rnd, section, question, settings, round_no)

    def info(self, qid: str) -> QInfo:
        return self._by_qid[qid]

    def round_size(self, round_id: str) -> int:
        return self._round_sizes[round_id]

    def position_in_round(self, plan: Plan, cursor: int) -> tuple[int, int]:
        """(номер питання в раунді, питань у раунді) для `plan[cursor]`, нумерація з 1."""
        round_id = self.info(plan[cursor]["qid"]).round.id
        before = sum(1 for e in plan[:cursor] if self.info(e["qid"]).round.id == round_id)
        return before + 1, self.round_size(round_id)

    def boundary_at(self, plan: Plan, cursor: int) -> Boundary | None:
        """Що починається на `cursor`: новий раунд, нова секція чи просто наступне питання."""
        if cursor == 0:
            return "round"
        prev, cur = self.info(plan[cursor - 1]["qid"]), self.info(plan[cursor]["qid"])
        if prev.round.id != cur.round.id:
            return "round"
        if prev.section.id != cur.section.id:
            return "section"
        return None


def build_plan(content: Content, rng: random.Random | None = None) -> Plan:
    """Раунди й секції — по порядку; усередині секції питання/варіанти — за налаштуваннями."""
    rng = rng or random.SystemRandom()
    plan: Plan = []
    for rnd in content.rounds:
        for section in rnd.sections:
            settings = content.settings_for(rnd, section)
            questions = list(section.questions)
            if settings.shuffle_questions:
                rng.shuffle(questions)
            for question in questions:
                size = len(question.options or []) if section.question_type == "choice" else 2
                order = list(range(size))
                if section.question_type == "choice" and settings.shuffle_options:
                    rng.shuffle(order)
                plan.append({"qid": question.id, "option_order": order})
    return plan
