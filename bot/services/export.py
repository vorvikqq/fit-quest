"""CSV-експорт для /export (FR-28): учасники, відповіді, відгуки.

Файли у UTF-8 з BOM, щоб Excel правильно показав кирилицю. Поля, що починаються з
`= + - @`, екрануються апострофом: імена вводять самі учасники (захист від CSV-ін'єкцій).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Answer, Attempt, User
from bot.services.plan import ContentIndex

_DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.startswith(_DANGEROUS):
        return "'" + value
    return value


def _to_csv(header: list[str], rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


async def build_exports(session: AsyncSession, index: ContentIndex) -> dict[str, bytes]:
    users = {u.tg_id: u for u in (await session.execute(select(User))).scalars()}
    attempts = list((await session.execute(select(Attempt).order_by(Attempt.id))).scalars())
    places = {r.attempt_id: r.place for r in await repo.leaderboard(session)}

    participants = []
    for user in users.values():
        if user.registered_at is None:
            continue
        a = next((x for x in attempts if x.tg_id == user.tg_id), None)
        participants.append(
            [
                user.tg_id, user.username, user.tg_name, user.full_name, user.status, user.course,
                user.registered_at, user.is_admin, user.excluded, user.blocked_bot,
                a.status if a else "", a.score if a else "", a.total_time_ms if a else "",
                a.finished_at if a else "", places.get(a.id, "") if a else "",
            ]
        )  # fmt: skip
    participants.sort(key=lambda r: (r[14] == "", r[14] or 0, r[0]))

    attempt_by_id = {a.id: a for a in attempts}
    answers = []
    rows = (
        await session.execute(select(Answer).order_by(Answer.attempt_id, Answer.answered_at))
    ).scalars()
    for ans in rows:
        a = attempt_by_id[ans.attempt_id]
        user = users[a.tg_id]
        try:
            question = index.info(ans.qid).question
            text, correct = question.text, question.correct
        except KeyError:  # питання прибрали з контенту після гри
            text, correct = "", ""
        answers.append(
            [a.tg_id, user.full_name or user.tg_name, ans.qid, text, ans.chosen, correct,
             ans.is_correct, ans.response_ms, ans.answered_at]
        )  # fmt: skip

    feedback = [
        [a.tg_id, users[a.tg_id].full_name or users[a.tg_id].tg_name, users[a.tg_id].username, a.feedback]
        for a in attempts
        if a.feedback  # порожній рядок = «пропустив»
    ]

    return {
        "participants.csv": _to_csv(
            ["tg_id", "username", "tg_name", "full_name", "status", "course", "registered_at",
             "is_admin", "excluded", "blocked_bot", "attempt_status", "score", "total_time_ms",
             "finished_at", "place"],
            participants,
        ),  # fmt: skip
        "answers.csv": _to_csv(
            ["tg_id", "full_name", "qid", "question", "chosen", "correct", "is_correct",
             "response_ms", "answered_at"],
            answers,
        ),  # fmt: skip
        "feedback.csv": _to_csv(["tg_id", "full_name", "username", "feedback"], feedback),
    }
