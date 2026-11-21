"""Завантаження `quest.yaml`, валідація й пошук TODO (DESIGN §5).

Запуск: `python -m bot.content.loader [шлях] [--strict]`
Код виходу: 1 — є помилки; 2 — немає помилок, але є TODO і задано `--strict`.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from bot.content.models import Content, Question
from bot.content.textutil import html_problems, plain, tg_len

CAPTION_LIMIT = 1024  # підпис до фото
MESSAGE_LIMIT = 4096  # звичайне повідомлення
CHOICE_MIN, CHOICE_MAX = 2, 4

# Ключі `messages`, які використовує бот (DESIGN §12 + ігровий цикл).
REQUIRED_MESSAGES = frozenset(
    {
        "welcome",
        "registration_closed",
        "wrong_code",
        "ask_name",
        "ask_status",
        "ask_course",
        "invalid_name",
        "too_many_attempts",
        "status_fit_student",
        "status_applicant",
        "status_other_student",
        "status_other",
        "course_1",
        "course_2",
        "course_3",
        "course_4",
        "course_master",
        "btn_start_quest",
        "btn_start_round",
        "question_header",
        "round_stats",
        "answer_stale",
        "time_left",
        "quest_not_started",
        "quest_closed_late",
        "btn_skip",
        "closing_soon",
        "review_right_answer",
        "review_explanation",
        "review_your_answer",
        "review_timeout",
        "results_no_place",
        "review_not_published",
        "review_no_attempt",
        "review_slow_down",
        "consent_note",
        "registered_waiting",
        "quest_started",
        "rules",
        "answer_accepted",
        "time_is_up",
        "use_buttons",
        "finished",
        "partners",
        "ask_feedback",
        "feedback_thanks",
        "quest_closed_unfinished",
        "results",
    }
)

# Запас на шапку «Раунд 3/5 · Питання 4/10 · ⏱ 15 с» (+ роздільники), яку бот додає до питання.
HEADER_RESERVE = 40
# Запас на дописку «✅ Відповідь прийнято» (render.SUFFIX_RESERVE).
SUFFIX_RESERVE = 30
RESERVE = HEADER_RESERVE + SUFFIX_RESERVE


class ContentError(Exception):
    """quest.yaml не вдалося прочитати або він має хибну форму."""


Level = Literal["error", "todo", "warning"]


@dataclass(frozen=True)
class Issue:
    level: Level
    where: str  # id питання / секції / раунду або "meta", "messages"
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.where}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level: Level, where: str, message: str) -> None:
        self.issues.append(Issue(level, where, message))

    def _of(self, level: Level) -> list[Issue]:
        return [i for i in self.issues if i.level == level]

    @property
    def errors(self) -> list[Issue]:
        return self._of("error")

    @property
    def todos(self) -> list[Issue]:
        return self._of("todo")

    @property
    def warnings(self) -> list[Issue]:
        return self._of("warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def has_todo(self) -> bool:
        """Блокує перехід у RUNNING (FR-30)."""
        return bool(self.todos)

    def render(self) -> str:
        if not self.issues:
            return "✅ Контент без зауважень."
        lines: list[str] = []
        for title, items in (
            ("Помилки", self.errors),
            ("Не заповнено (TODO)", self.todos),
            ("Попередження", self.warnings),
        ):
            if items:
                lines.append(f"{title}: {len(items)}")
                lines.extend(f"  {i.where}: {i.message}" for i in items)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Допоміжні функції
# ---------------------------------------------------------------------------


def _is_todo_text(text: str | None) -> bool:
    return bool(text) and plain(text).strip().upper().startswith("TODO")


def rendered_length(question: Question, question_type: str) -> int:
    """Довжина тексту питання разом з варіантами `A) …`, як їх покаже бот (§6.3).

    Текст питання містить HTML (теги не рахуються), варіанти бот екранує, тож вони
    рахуються як є. Шапку раунду додає `HEADER_RESERVE` у перевірці лімітів.
    """
    parts = [plain(question.text)]
    if question_type == "choice" and question.options:
        letters = "ABCD"
        parts.extend(f"{letters[i]}) {opt}" for i, opt in enumerate(question.options[:CHOICE_MAX]))
    return tg_len("\n".join(parts))


# ---------------------------------------------------------------------------
# Валідація
# ---------------------------------------------------------------------------


def validate_content(content: Content, root: Path) -> Report:
    """Перевіряє правила §5. `root` — каталог `content/` (від нього шляхи `image`)."""
    report = Report()
    _check_meta_and_messages(content, report)
    _check_unique_ids(content, report)

    for rnd in content.rounds:
        if not rnd.sections:
            report.add("error", rnd.id, "раунд без секцій")
        if _is_todo_text(rnd.title) or _is_todo_text(rnd.intro):
            report.add("todo", rnd.id, "назва або вступ раунду починається з TODO")
        for section in rnd.sections:
            _check_section(content, rnd.id, section, root, report)

    return report


def _check_meta_and_messages(content: Content, report: Report) -> None:
    if not content.rounds:
        report.add("error", "rounds", "немає жодного раунду")
    if _is_todo_text(content.meta.promo.text) or not content.meta.promo.code.strip():
        report.add("error", "meta.promo", "порожній промокод або текст промокоду")
    if content.meta.defaults.time_limit <= 0:
        report.add("error", "meta.defaults", "time_limit має бути > 0")

    for key in sorted(REQUIRED_MESSAGES - content.messages.keys()):
        report.add("error", f"messages.{key}", "відсутній обов'язковий текст")
    for key, text in content.messages.items():
        if not text.strip():
            report.add("error", f"messages.{key}", "порожній текст")
        elif _is_todo_text(text):
            report.add("todo", f"messages.{key}", "текст починається з TODO")


def _check_unique_ids(content: Content, report: Report) -> None:
    def duplicates(ids: list[str]) -> list[str]:
        return sorted(i for i, n in Counter(ids).items() if n > 1)

    rounds = [r.id for r in content.rounds]
    sections = [s.id for r in content.rounds for s in r.sections]
    questions = [q.id for _, _, q in content.iter_questions()]
    for kind, ids in (("раунду", rounds), ("секції", sections), ("питання", questions)):
        for dup in duplicates(ids):
            report.add("error", dup, f"id {kind} повторюється")


def _check_section(content: Content, round_id: str, section, root: Path, report: Report) -> None:
    rnd = next(r for r in content.rounds if r.id == round_id)
    settings = content.settings_for(rnd, section)

    if settings.time_limit <= 0:
        report.add("error", section.id, "time_limit має бути > 0")
    if not section.questions:
        report.add("error", section.id, "секція без питань")
    if _is_todo_text(section.intro):
        report.add("todo", section.id, "вступ секції починається з TODO")

    if section.question_type == "binary":
        if section.labels is None or len(section.labels) != 2:
            report.add("error", section.id, "для binary потрібно рівно 2 `labels`")
        if any(not label.strip() for label in section.labels or []):
            report.add("error", section.id, "порожня мітка в `labels`")
    elif section.labels is not None:
        report.add("error", section.id, "`labels` має сенс лише для binary")

    for question in section.questions:
        _check_question(section.question_type, question, root, report)


def _check_question(question_type: str, q: Question, root: Path, report: Report) -> None:
    if not q.text.strip():
        report.add("error", q.id, "порожній текст питання")

    # --- форма варіантів і правильна відповідь -------------------------
    if question_type == "choice":
        options = q.options or []
        if not CHOICE_MIN <= len(options) <= CHOICE_MAX:
            report.add(
                "error", q.id, f"choice має {len(options)} варіантів, потрібно {CHOICE_MIN}–{CHOICE_MAX}"
            )
        if any(not opt.strip() for opt in options):
            report.add("error", q.id, "порожній варіант відповіді")
        size = len(options)
    else:
        if q.options is not None:
            report.add("error", q.id, "`options` не використовується в binary")
        size = 2

    if q.correct is not None and not 0 <= q.correct < size:
        report.add("error", q.id, f"correct={q.correct} поза межами 0..{size - 1}")

    # --- картинка -----------------------------------------------------
    if q.image is not None and not (root / q.image).is_file():
        report.add("error", q.id, f"файл картинки не знайдено: {q.image}")

    # --- ліміти Telegram ----------------------------------------------
    length = rendered_length(q, question_type) + RESERVE
    if length > MESSAGE_LIMIT:
        report.add("error", q.id, f"{length} символів, ліміт повідомлення {MESSAGE_LIMIT}")
    elif q.image is not None and length > CAPTION_LIMIT:
        # Не помилка: §6.3 надсилає картинку й питання двома повідомленнями.
        report.add(
            "warning",
            q.id,
            f"{length} символів > {CAPTION_LIMIT} (ліміт підпису): "
            "бот надішле картинку окремим повідомленням",
        )

    # --- незаповнене (TODO) -------------------------------------------
    # Одне питання — один запис, навіть якщо причин кілька.
    reasons: list[str] = []
    for problem in html_problems(q.text):
        report.add("error", q.id, f"HTML у тексті питання: {problem}")
    if q.todo and q.todo.strip():
        reasons.append(q.todo.strip())
    if q.correct is None:
        reasons.append("не вказано `correct`")
    if _is_todo_text(q.text) or any(_is_todo_text(o) for o in q.options or []):
        reasons.append("текст або варіант починається з TODO")
    if _is_todo_text(q.explanation):
        reasons.append("пояснення починається з TODO")
    if reasons:
        report.add("todo", q.id, "; ".join(reasons))


# ---------------------------------------------------------------------------
# Завантаження
# ---------------------------------------------------------------------------


def parse_content(raw: object) -> Content:
    """dict (з YAML) → `Content`. Хибну форму перетворює на `ContentError`."""
    try:
        return Content.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ContentError(f"quest.yaml має хибну структуру: {details}") from exc


def load_content(path: Path) -> Content:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContentError(f"не вдалося прочитати {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContentError(f"некоректний YAML у {path}: {exc}") from exc
    return parse_content(raw)


def file_sha256(path: Path) -> str:
    """Хеш quest.yaml для `game_state.content_hash` (захист від правок посеред гри)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_validate(path: Path) -> tuple[Content, Report]:
    """Читає файл і перевіряє його; картинки шукає поруч із файлом."""
    content = load_content(path)
    return content, validate_content(content, path.parent)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from bot.config import PROJECT_ROOT

    parser = argparse.ArgumentParser(description="Валідація content/quest.yaml")
    parser.add_argument("path", nargs="?", type=Path, default=PROJECT_ROOT / "content" / "quest.yaml")
    parser.add_argument("--strict", action="store_true", help="TODO теж вважати збоєм")
    args = parser.parse_args(argv)

    try:
        content, report = load_and_validate(args.path)
    except ContentError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(f"Раундів: {len(content.rounds)}, питань: {content.question_count}")
    print(report.render())
    if report.errors:
        return 1
    if args.strict and report.has_todo:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
