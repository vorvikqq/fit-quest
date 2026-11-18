"""Синтетичний контент без TODO та підроблений месенджер для тестів ігрового ядра."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from bot.config import PROJECT_ROOT
from bot.content.loader import parse_content
from bot.content.models import Content
from bot.db import repo
from bot.services.render import QuestionView

LONG = "дуже довгий варіант відповіді " * 12  # ~360 символів

ROUNDS: list[dict[str, Any]] = [
    {
        "id": "r1",
        "title": "Раунд Один",
        "intro": "вступ раунду 1",
        "time_limit": 10,
        "shuffle_options": True,
        "sections": [
            {
                "id": "s1",
                "question_type": "choice",
                "questions": [
                    {"id": "q1", "text": "Питання 1", "options": ["a", "b", "c", "d"], "correct": 1},
                    {"id": "q2", "text": "Питання 2", "options": ["x", "y", "z"], "correct": 0},
                ],
            }
        ],
    },
    {
        "id": "r2",
        "title": "Раунд Два",
        "intro": "вступ раунду 2",
        "time_limit": 5,
        "sections": [
            {
                "id": "s2a",
                "intro": "вступ секції 2a",
                "question_type": "binary",
                "labels": ["Так", "Ні"],
                "questions": [
                    {"id": "q3", "text": "Питання 3", "image": "images/r5_photo_01.png", "correct": 0},
                    {"id": "q4", "text": "Питання 4", "correct": 1},
                ],
            },
            {
                "id": "s2b",
                "intro": "вступ секції 2b",
                "question_type": "choice",
                "time_limit": 7,
                "questions": [
                    {
                        "id": "q5",
                        "text": "Питання 5",
                        "image": "images/r5_prompt_01.png",
                        "options": [LONG + "1", LONG + "2", LONG + "3", LONG + "4"],
                        "correct": 2,
                    }
                ],
            },
        ],
    },
]
TOTAL = 5


def make_content(rounds: list[dict[str, Any]] | None = None) -> Content:
    raw = yaml.safe_load((PROJECT_ROOT / "content" / "quest.yaml").read_text(encoding="utf-8"))
    raw["rounds"] = copy.deepcopy(rounds or ROUNDS)
    return parse_content(raw)


class Clock:
    """Керований годинник: тест сам «проматує» час."""

    def __init__(self) -> None:
        self.now = datetime(2026, 11, 20, 18, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeMessenger:
    """Записує все, що бот «надіслав». `blocked` — користувачі, яким нічого не доходить."""

    events: list[tuple] = field(default_factory=list)
    blocked: set[int] = field(default_factory=set)
    _next_id: int = 1

    def _id(self, tg_id: int) -> int | None:
        if tg_id in self.blocked:
            return None
        self._next_id += 1
        return self._next_id

    async def send_text(self, tg_id: int, text: str) -> int | None:
        self.events.append(("text", tg_id, text))
        return self._id(tg_id)

    async def send_card(self, tg_id: int, text: str, cursor: int) -> int | None:
        self.events.append(("card", tg_id, text, cursor))
        return self._id(tg_id)

    async def send_question(self, tg_id: int, view: QuestionView, cursor: int) -> int | None:
        self.events.append(("question", tg_id, view, cursor))
        return self._id(tg_id)

    async def close_question(self, tg_id: int, message_id, view: QuestionView, suffix: str) -> None:
        self.events.append(("close", tg_id, message_id, suffix))

    async def remove_keyboard(self, tg_id: int, message_id) -> None:
        self.events.append(("remove_kb", tg_id, message_id))

    # --- зручні вибірки ---------------------------------------------------

    def of(self, kind: str, tg_id: int | None = None) -> list[tuple]:
        return [e for e in self.events if e[0] == kind and (tg_id is None or e[1] == tg_id)]

    def questions(self, tg_id: int) -> list[tuple]:
        return self.of("question", tg_id)

    def cards(self, tg_id: int) -> list[tuple]:
        return self.of("card", tg_id)

    def clear(self) -> None:
        self.events.clear()


async def register_users(session_factory, *ids: int) -> None:
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s, "CODE")
        for uid in ids:
            await repo.upsert_user(s, uid, f"u{uid}", f"User {uid}")
            await repo.register_user(s, uid, f"User {uid}", "OTHER", None, datetime.now(UTC))


async def set_running(session_factory, **fields: Any) -> None:
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s, "CODE")
        await repo.update_game_state(s, phase="RUNNING", registration_open=True, **fields)
