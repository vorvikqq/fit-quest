"""Усі запити до БД. Жодного SQL у хендлерах і сервісах (DESIGN §2).

Функції приймають `AsyncSession` і **не комітять**: межу транзакції задає
викликач (`async with session.begin():`), щоб прийом відповіді був однією
транзакцією (DESIGN §6.4).

Переходи стану спроби — атомарні `UPDATE … WHERE cursor = :cursor`.
Якщо повернулось `None`/`False`, відповідь застаріла або це дубль.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Answer, Attempt, AttemptStatus, GameState, MediaCache, Phase, User

# ---------------------------------------------------------------------------
# game_state (один рядок, id = 1)
# ---------------------------------------------------------------------------


async def ensure_game_state(session: AsyncSession, access_code: str = "") -> GameState:
    """Створює рядок `game_state`, якщо його ще немає (код береться з .env)."""
    await session.execute(
        insert(GameState)
        .values(id=1, phase=Phase.DRAFT.value, registration_open=False, access_code=access_code)
        .on_conflict_do_nothing(index_elements=[GameState.id])
    )
    return await get_game_state(session)


async def get_game_state(session: AsyncSession, lock: bool = False) -> GameState:
    """`lock=True` → `SELECT … FOR UPDATE`: переходи етапів не конкурують між собою."""
    stmt = select(GameState).where(GameState.id == 1).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one()


async def update_game_state(session: AsyncSession, **fields: Any) -> GameState:
    if fields:
        await session.execute(update(GameState).where(GameState.id == 1).values(**fields))
    return await get_game_state(session)


async def transition_phase(
    session: AsyncSession, expected: Phase, new: Phase, **fields: Any
) -> bool:
    """CAS переходу етапу: спрацює, лише якщо поточний етап = `expected`."""
    result = await session.execute(
        update(GameState)
        .where(GameState.id == 1, GameState.phase == expected.value)
        .values(phase=new.value, **fields)
    )
    return result.rowcount == 1


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


async def upsert_user(
    session: AsyncSession, tg_id: int, username: str | None, tg_name: str, is_admin: bool = False
) -> User:
    """Створює користувача або оновлює його Telegram-дані (реєстраційні поля не чіпає)."""
    stmt = insert(User).values(tg_id=tg_id, username=username, tg_name=tg_name, is_admin=is_admin)
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.tg_id],
        set_={"username": stmt.excluded.username, "tg_name": stmt.excluded.tg_name, "is_admin": is_admin},
    )
    await session.execute(stmt)
    return await get_user(session, tg_id)  # type: ignore[return-value]


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.get(User, tg_id, populate_existing=True)


async def find_user(session: AsyncSession, ref: str) -> User | None:
    """`tg_id` або `@username` (для /user, /exclude, /include)."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        return await get_user(session, int(ref))
    username = ref.removeprefix("@").lower()
    stmt = select(User).where(User.username.ilike(username))
    return (await session.execute(stmt)).scalars().first()


async def register_user(
    session: AsyncSession,
    tg_id: int,
    full_name: str,
    status: str,
    course: str | None,
    now: datetime,
) -> bool:
    """Завершує реєстрацію. `False`, якщо користувач уже зареєстрований (FR-6)."""
    result = await session.execute(
        update(User)
        .where(User.tg_id == tg_id, User.registered_at.is_(None))
        .values(full_name=full_name, status=status, course=course, registered_at=now)
    )
    return result.rowcount == 1


async def set_user_flag(session: AsyncSession, tg_id: int, **flags: bool) -> bool:
    """`excluded`, `blocked_bot`, `is_admin`."""
    result = await session.execute(update(User).where(User.tg_id == tg_id).values(**flags))
    return result.rowcount == 1


async def list_registered_ids(session: AsyncSession, include_blocked: bool = False) -> list[int]:
    stmt = select(User.tg_id).where(User.registered_at.is_not(None))
    if not include_blocked:
        stmt = stmt.where(User.blocked_bot.is_(False))
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# attempts
# ---------------------------------------------------------------------------


async def create_attempt(
    session: AsyncSession, tg_id: int, plan: list[dict[str, Any]], now: datetime
) -> Attempt | None:
    """Стартує спробу. `None`, якщо вона вже є (UNIQUE tg_id, DESIGN §6.1)."""
    stmt = (
        insert(Attempt)
        .values(
            tg_id=tg_id,
            status=AttemptStatus.ROUND_INTRO.value,
            started_at=now,
            plan=plan,
            cursor=0,
        )
        .on_conflict_do_nothing(index_elements=[Attempt.tg_id])
        .returning(Attempt)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_attempt(session: AsyncSession, attempt_id: int) -> Attempt | None:
    return await session.get(Attempt, attempt_id, populate_existing=True)


async def get_attempt_for_user(session: AsyncSession, tg_id: int) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.tg_id == tg_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_question_sent(
    session: AsyncSession,
    attempt_id: int,
    cursor: int,
    sent_at: datetime,
    deadline: datetime,
    message_id: int | None,
) -> bool:
    """Фіксує момент надсилання питання (§6.3, крок 3).

    Спрацює один раз на питання: умова `q_sent_at IS NULL` не дає перезаписати
    дедлайн при повторному надсиланні. Повторна відправка того самого питання
    після `/start` іде з уже збереженим дедлайном — див. `set_question_message_id`.
    """
    result = await session.execute(
        update(Attempt)
        .where(
            Attempt.id == attempt_id,
            Attempt.cursor == cursor,
            Attempt.status.in_([AttemptStatus.ROUND_INTRO.value, AttemptStatus.QUESTION.value]),
            Attempt.q_sent_at.is_(None),
        )
        .values(
            status=AttemptStatus.QUESTION.value,
            q_sent_at=sent_at,
            q_deadline=deadline,
            q_message_id=message_id,
        )
    )
    return result.rowcount == 1


async def set_question_message_id(
    session: AsyncSession, attempt_id: int, cursor: int, message_id: int
) -> bool:
    result = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt_id, Attempt.cursor == cursor)
        .values(q_message_id=message_id)
    )
    return result.rowcount == 1


async def advance_attempt(
    session: AsyncSession,
    attempt_id: int,
    cursor: int,
    new_status: AttemptStatus,
    score_delta: int = 0,
    time_delta_ms: int = 0,
    finished_at: datetime | None = None,
) -> Attempt | None:
    """Головний механізм ідемпотентності (NFR-4, DESIGN §6.4).

    ```
    UPDATE attempts SET cursor = cursor + 1, status = … , score += …
    WHERE id = :id AND cursor = :cursor AND status = 'QUESTION'
    RETURNING …
    ```
    Повертає оновлену спробу (новий `cursor`/`status`/`score`) або `None`, якщо
    відповідь застаріла чи це дубль. Прийом відповіді й таймаут викликають одну й ту
    саму функцію: перемагає той, хто перший змінив `cursor`.

    `elapsed` викликач рахує зі знімка спроби, прочитаного до CAS: `q_sent_at` для
    цього курсора не змінюється (див. `mark_question_sent`), тож знімок надійний.

    Дані питання (`q_*`) скидаються тут же, щоб сторож не закрив ще не надіслане
    наступне питання.
    """
    stmt = (
        update(Attempt)
        .where(
            Attempt.id == attempt_id,
            Attempt.cursor == cursor,
            Attempt.status == AttemptStatus.QUESTION.value,
        )
        .values(
            cursor=Attempt.cursor + 1,
            status=new_status.value,
            score=Attempt.score + score_delta,
            total_time_ms=Attempt.total_time_ms + time_delta_ms,
            finished_at=finished_at,
            q_sent_at=None,
            q_deadline=None,
            q_message_id=None,
        )
        .returning(Attempt)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_answer(
    session: AsyncSession,
    attempt_id: int,
    qid: str,
    chosen: int | None,
    is_correct: bool,
    response_ms: int,
    answered_at: datetime,
) -> bool:
    """`False`, якщо відповідь на це питання вже є (PK attempt_id+qid, NFR-4)."""
    stmt = (
        insert(Answer)
        .values(
            attempt_id=attempt_id,
            qid=qid,
            chosen=chosen,
            is_correct=is_correct,
            response_ms=response_ms,
            answered_at=answered_at,
        )
        .on_conflict_do_nothing(index_elements=[Answer.attempt_id, Answer.qid])
    )
    return (await session.execute(stmt)).rowcount == 1


async def list_answers(session: AsyncSession, attempt_id: int) -> Sequence[Answer]:
    stmt = select(Answer).where(Answer.attempt_id == attempt_id).order_by(Answer.answered_at)
    return (await session.execute(stmt)).scalars().all()


async def set_attempt_status(
    session: AsyncSession, attempt_id: int, expected: AttemptStatus, new: AttemptStatus
) -> bool:
    """CAS зміни статусу без руху курсора (наприклад, ROUND_INTRO → QUESTION)."""
    result = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt_id, Attempt.status == expected.value)
        .values(status=new.value)
    )
    return result.rowcount == 1


async def list_active_questions(session: AsyncSession) -> Sequence[Attempt]:
    """Recovery при старті (§7): усі спроби, що чекають відповіді."""
    stmt = select(Attempt).where(
        Attempt.status == AttemptStatus.QUESTION.value, Attempt.q_deadline.is_not(None)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_unsent_questions(session: AsyncSession) -> Sequence[Attempt]:
    """`QUESTION` без дедлайну: бот упав між прийомом відповіді й надсиланням наступного."""
    stmt = select(Attempt).where(
        Attempt.status == AttemptStatus.QUESTION.value, Attempt.q_deadline.is_(None)
    )
    return (await session.execute(stmt)).scalars().all()


async def list_stale_questions(
    session: AsyncSession, now: datetime, grace: timedelta = timedelta(seconds=3)
) -> Sequence[Attempt]:
    """Сторож (§7): дедлайн минув більш ніж на `grace`, а таймер не спрацював."""
    stmt = select(Attempt).where(
        Attempt.status == AttemptStatus.QUESTION.value, Attempt.q_deadline < now - grace
    )
    return (await session.execute(stmt)).scalars().all()


async def close_unfinished(session: AsyncSession) -> Sequence[Attempt]:
    """`/close_quest` (§6.7): усі спроби в ROUND_INTRO/QUESTION → CLOSED_UNFINISHED."""
    stmt = (
        update(Attempt)
        .where(Attempt.status.in_([AttemptStatus.ROUND_INTRO.value, AttemptStatus.QUESTION.value]))
        .values(status=AttemptStatus.CLOSED_UNFINISHED.value)
        .returning(Attempt)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().all()


async def mark_promo_sent(session: AsyncSession, attempt_id: int) -> bool:
    """`True` лише для того, хто перший виставив прапорець (промокод шлемо один раз)."""
    result = await session.execute(
        update(Attempt)
        .where(Attempt.id == attempt_id, Attempt.promo_sent.is_(False))
        .values(promo_sent=True)
    )
    return result.rowcount == 1


async def set_feedback(session: AsyncSession, tg_id: int, feedback: str) -> None:
    await session.execute(
        update(Attempt).where(Attempt.tg_id == tg_id).values(feedback=feedback[:300])
    )


@dataclass(frozen=True)
class Counters:
    registered: int
    in_progress: int  # ROUND_INTRO + QUESTION
    finished: int
    closed_unfinished: int


async def get_counters(session: AsyncSession) -> Counters:
    """Лічильники для /admin (повний /stats — окремо, FR-26)."""
    registered = (
        await session.execute(select(func.count()).where(User.registered_at.is_not(None)))
    ).scalar_one()
    rows = (
        await session.execute(select(Attempt.status, func.count()).group_by(Attempt.status))
    ).all()
    by_status = {status: n for status, n in rows}
    return Counters(
        registered=registered,
        in_progress=by_status.get(AttemptStatus.ROUND_INTRO.value, 0)
        + by_status.get(AttemptStatus.QUESTION.value, 0),
        finished=by_status.get(AttemptStatus.FINISHED.value, 0),
        closed_unfinished=by_status.get(AttemptStatus.CLOSED_UNFINISHED.value, 0),
    )


# ---------------------------------------------------------------------------
# media_cache
# ---------------------------------------------------------------------------


async def get_media(session: AsyncSession, path: str) -> MediaCache | None:
    return await session.get(MediaCache, path, populate_existing=True)


async def set_media(session: AsyncSession, path: str, sha256: str, file_id: str) -> None:
    stmt = insert(MediaCache).values(path=path, sha256=sha256, file_id=file_id)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MediaCache.path],
        set_={"sha256": stmt.excluded.sha256, "file_id": stmt.excluded.file_id},
    )
    await session.execute(stmt)
