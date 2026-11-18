from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError

from bot.db import repo
from bot.db.models import AttemptStatus, Base, GameState, Phase

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
PLAN = [{"qid": "q1", "option_order": [1, 0]}, {"qid": "q2", "option_order": [0, 1]}]


async def make_attempt(session_factory, tg_id: int = 1):
    async with session_factory.begin() as s:
        await repo.upsert_user(s, tg_id, "user", "Імʼя")
        attempt = await repo.create_attempt(s, tg_id, PLAN, NOW)
    return attempt


async def start_question(session_factory, attempt, limit: int = 20, sent_at: datetime = NOW):
    async with session_factory.begin() as s:
        ok = await repo.mark_question_sent(
            s, attempt.id, attempt.cursor, sent_at, sent_at + timedelta(seconds=limit), 555
        )
    assert ok


# --- схема ----------------------------------------------------------------


async def test_migration_matches_models(engine):
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
        )
    assert diff == []


# --- game_state -----------------------------------------------------------


async def test_game_state_is_single_row(session_factory):
    async with session_factory.begin() as s:
        first = await repo.ensure_game_state(s, "CODE")
        again = await repo.ensure_game_state(s, "OTHER")  # не перезаписує
    assert first.phase == Phase.DRAFT and again.access_code == "CODE"
    assert again.registration_open is False

    async with session_factory() as s:
        s.add(GameState(id=2))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_phase_transition_is_cas(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
        assert await repo.transition_phase(s, Phase.DRAFT, Phase.REGISTRATION, registration_open=True)
        # повторний виклик з тим самим expected — уже не спрацьовує
        assert not await repo.transition_phase(s, Phase.DRAFT, Phase.REGISTRATION)
        state = await repo.get_game_state(s)
    assert state.phase == Phase.REGISTRATION and state.registration_open


# --- users ----------------------------------------------------------------


async def test_upsert_keeps_registration_data(session_factory):
    async with session_factory.begin() as s:
        await repo.upsert_user(s, 10, "old", "Old Name")
        await repo.register_user(s, 10, "Іван Петренко", "FIT_STUDENT", "2", NOW)
    async with session_factory.begin() as s:
        user = await repo.upsert_user(s, 10, "new", "New Name")
    assert (user.username, user.tg_name) == ("new", "New Name")
    assert (user.full_name, user.status, user.course) == ("Іван Петренко", "FIT_STUDENT", "2")


async def test_find_user_by_id_and_username(session_factory):
    async with session_factory.begin() as s:
        await repo.upsert_user(s, 10, "SomeUser", "A")
    async with session_factory() as s:
        assert (await repo.find_user(s, "10")).tg_id == 10
        assert (await repo.find_user(s, "@someuser")).tg_id == 10
        assert await repo.find_user(s, "@nobody") is None


async def test_invalid_status_rejected(session_factory):
    async with session_factory() as s:
        await repo.upsert_user(s, 10, None, "A")
        with pytest.raises(IntegrityError):
            await repo.register_user(s, 10, "Name", "ROBOT", None, NOW)


async def test_list_registered_skips_blocked(session_factory):
    async with session_factory.begin() as s:
        for uid in (1, 2, 3):
            await repo.upsert_user(s, uid, None, "n")
        await repo.register_user(s, 1, "A B", "OTHER", None, NOW)
        await repo.register_user(s, 2, "C D", "OTHER", None, NOW)
        await repo.set_user_flag(s, 2, blocked_bot=True)
    async with session_factory() as s:
        assert await repo.list_registered_ids(s) == [1]
        assert sorted(await repo.list_registered_ids(s, include_blocked=True)) == [1, 2]


# --- attempts -------------------------------------------------------------


async def test_one_attempt_per_user(session_factory):
    first = await make_attempt(session_factory)
    async with session_factory.begin() as s:
        second = await repo.create_attempt(s, 1, PLAN, NOW)
    assert first is not None and second is None
    assert first.status == AttemptStatus.ROUND_INTRO and first.plan == PLAN


async def test_mark_question_sent_only_once(session_factory):
    attempt = await make_attempt(session_factory)
    await start_question(session_factory, attempt)
    async with session_factory.begin() as s:
        again = await repo.mark_question_sent(
            s, attempt.id, 0, NOW + timedelta(seconds=5), NOW + timedelta(seconds=25), 777
        )
        stored = await repo.get_attempt(s, attempt.id)
    assert again is False
    assert stored.q_deadline == NOW + timedelta(seconds=20) and stored.q_message_id == 555


async def test_advance_applies_score_and_time(session_factory):
    attempt = await make_attempt(session_factory)
    await start_question(session_factory, attempt)
    async with session_factory.begin() as s:
        updated = await repo.advance_attempt(
            s, attempt.id, 0, AttemptStatus.QUESTION, score_delta=1, time_delta_ms=3200
        )
    assert (updated.cursor, updated.score, updated.total_time_ms) == (1, 1, 3200)
    # q_* скинуті: сторож не чіпатиме ще не надіслане питання
    assert updated.q_sent_at is None and updated.q_deadline is None


async def test_stale_or_wrong_cursor_is_rejected(session_factory):
    attempt = await make_attempt(session_factory)
    await start_question(session_factory, attempt)
    async with session_factory.begin() as s:
        assert await repo.advance_attempt(s, attempt.id, 5, AttemptStatus.QUESTION) is None
        assert await repo.advance_attempt(s, attempt.id, 0, AttemptStatus.QUESTION) is not None
        # те саме натискання вдруге — дубль
        assert await repo.advance_attempt(s, attempt.id, 0, AttemptStatus.QUESTION) is None


async def test_advance_requires_question_status(session_factory):
    attempt = await make_attempt(session_factory)  # ROUND_INTRO
    async with session_factory.begin() as s:
        assert await repo.advance_attempt(s, attempt.id, 0, AttemptStatus.QUESTION) is None


async def test_concurrent_answer_and_timeout_count_once(session_factory):
    """NFR-4: натискання й таймер одночасно → зараховується рівно одне."""
    attempt = await make_attempt(session_factory)
    await start_question(session_factory, attempt)

    async def contender(score_delta: int):
        async with session_factory.begin() as s:
            won = await repo.advance_attempt(
                s, attempt.id, 0, AttemptStatus.QUESTION, score_delta=score_delta
            )
            if won:
                await repo.insert_answer(s, attempt.id, "q1", 0, bool(score_delta), 100, NOW)
            return won

    results = await asyncio.gather(*(contender(1) for _ in range(10)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1

    async with session_factory() as s:
        stored = await repo.get_attempt(s, attempt.id)
        answers = await repo.list_answers(s, attempt.id)
    assert (stored.cursor, stored.score) == (1, 1) and len(answers) == 1


async def test_duplicate_answer_is_ignored(session_factory):
    attempt = await make_attempt(session_factory)
    async with session_factory.begin() as s:
        assert await repo.insert_answer(s, attempt.id, "q1", 1, True, 500, NOW)
        assert not await repo.insert_answer(s, attempt.id, "q1", 0, False, 900, NOW)
        answers = await repo.list_answers(s, attempt.id)
    assert [(a.chosen, a.is_correct) for a in answers] == [(1, True)]


async def test_timeout_answer_has_null_chosen(session_factory):
    attempt = await make_attempt(session_factory)
    async with session_factory.begin() as s:
        await repo.insert_answer(s, attempt.id, "q1", None, False, 20000, NOW)
        (answer,) = await repo.list_answers(s, attempt.id)
    assert answer.chosen is None


async def test_recovery_and_watchdog_queries(session_factory):
    a1 = await make_attempt(session_factory, 1)
    a2 = await make_attempt(session_factory, 2)
    a3 = await make_attempt(session_factory, 3)  # у ROUND_INTRO — не чіпаємо
    await start_question(session_factory, a1, limit=20, sent_at=NOW)  # дедлайн NOW+20
    await start_question(session_factory, a2, limit=20, sent_at=NOW + timedelta(minutes=5))

    async with session_factory() as s:
        active = {a.id for a in await repo.list_active_questions(s)}
        assert active == {a1.id, a2.id}
        # дедлайн a1 минув на 2 с — ще в межах пільгового періоду (3 с)
        at = NOW + timedelta(seconds=22)
        assert await repo.list_stale_questions(s, at) == []
        at = NOW + timedelta(seconds=24)
        assert {a.id for a in await repo.list_stale_questions(s, at)} == {a1.id}
    assert a3.id not in active


async def test_unsent_question_after_advance_is_not_stale(session_factory):
    attempt = await make_attempt(session_factory)
    await start_question(session_factory, attempt)
    async with session_factory.begin() as s:
        await repo.advance_attempt(s, attempt.id, 0, AttemptStatus.QUESTION)
    async with session_factory() as s:
        far_future = NOW + timedelta(days=1)
        assert await repo.list_stale_questions(s, far_future) == []
        assert [a.id for a in await repo.list_unsent_questions(s)] == [attempt.id]


async def test_close_unfinished(session_factory):
    a1 = await make_attempt(session_factory, 1)
    a2 = await make_attempt(session_factory, 2)
    a3 = await make_attempt(session_factory, 3)
    await start_question(session_factory, a2)
    async with session_factory.begin() as s:
        await repo.advance_attempt(s, a2.id, 0, AttemptStatus.QUESTION)
        await repo.set_attempt_status(s, a3.id, AttemptStatus.ROUND_INTRO, AttemptStatus.FINISHED)
    async with session_factory.begin() as s:
        closed = await repo.close_unfinished(s)
    assert {a.id for a in closed} == {a1.id, a2.id}
    assert all(a.status == AttemptStatus.CLOSED_UNFINISHED for a in closed)
    async with session_factory() as s:
        assert (await repo.get_attempt(s, a3.id)).status == AttemptStatus.FINISHED


async def test_promo_sent_flag_flips_once(session_factory):
    attempt = await make_attempt(session_factory)
    async with session_factory.begin() as s:
        assert await repo.mark_promo_sent(s, attempt.id)
        assert not await repo.mark_promo_sent(s, attempt.id)


async def test_feedback_is_truncated(session_factory):
    await make_attempt(session_factory)
    async with session_factory.begin() as s:
        await repo.set_feedback(s, 1, "я" * 500)
        stored = await repo.get_attempt_for_user(s, 1)
    assert len(stored.feedback) == 300


# --- media_cache ----------------------------------------------------------


async def test_media_cache_upsert(session_factory):
    async with session_factory.begin() as s:
        await repo.set_media(s, "images/a.png", "aaa", "FILE1")
        await repo.set_media(s, "images/a.png", "bbb", "FILE2")
        cached = await repo.get_media(s, "images/a.png")
    assert (cached.sha256, cached.file_id) == ("bbb", "FILE2")
    async with session_factory() as s:
        assert await repo.get_media(s, "missing") is None
