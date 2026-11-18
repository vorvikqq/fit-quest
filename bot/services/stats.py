"""Статистика для /stats (FR-26) і картка учасника для /user."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Integer, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Answer, Attempt, AttemptStatus, User


@dataclass
class HardQuestion:
    qid: str
    answered: int
    correct: int

    @property
    def percent(self) -> int:
        return round(100 * self.correct / self.answered) if self.answered else 0


@dataclass
class Stats:
    registered: int = 0
    in_progress: int = 0
    finished: int = 0
    closed_unfinished: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_course: dict[str, int] = field(default_factory=dict)
    avg_score: float | None = None  # серед тих, хто завершив
    hardest: list[HardQuestion] = field(default_factory=list)


async def collect_stats(session: AsyncSession, hardest_limit: int = 3) -> Stats:
    """Усе без адмінів: їхні тестові проходження не повинні спотворювати цифри."""
    stats = Stats()
    participants = User.is_admin.is_(False)

    stats.registered = (
        await session.execute(
            select(func.count()).where(User.registered_at.is_not(None), participants)
        )
    ).scalar_one()

    rows = await session.execute(
        select(User.status, func.count())
        .where(User.registered_at.is_not(None), participants)
        .group_by(User.status)
    )
    stats.by_status = {status: n for status, n in rows}

    rows = await session.execute(
        select(User.course, func.count())
        .where(User.registered_at.is_not(None), participants, User.course.is_not(None))
        .group_by(User.course)
    )
    stats.by_course = {course: n for course, n in rows}

    rows = await session.execute(
        select(Attempt.status, func.count(), func.avg(Attempt.score))
        .join(User, User.tg_id == Attempt.tg_id)
        .where(participants)
        .group_by(Attempt.status)
    )
    for status, count, avg in rows:
        if status in (AttemptStatus.ROUND_INTRO.value, AttemptStatus.QUESTION.value):
            stats.in_progress += count
        elif status == AttemptStatus.FINISHED.value:
            stats.finished = count
            stats.avg_score = float(avg) if avg is not None else None
        elif status == AttemptStatus.CLOSED_UNFINISHED.value:
            stats.closed_unfinished = count

    correct_count = func.sum(case((Answer.is_correct.is_(True), 1), else_=0).cast(Integer))
    rate = correct_count * 1.0 / func.count()
    rows = await session.execute(
        select(Answer.qid, func.count(), correct_count)
        .join(Attempt, Attempt.id == Answer.attempt_id)
        .join(User, User.tg_id == Attempt.tg_id)
        .where(participants)
        .group_by(Answer.qid)
        .order_by(rate.asc(), func.count().desc(), Answer.qid.asc())
        .limit(hardest_limit)
    )
    stats.hardest = [HardQuestion(qid, answered, int(correct or 0)) for qid, answered, correct in rows]
    return stats
