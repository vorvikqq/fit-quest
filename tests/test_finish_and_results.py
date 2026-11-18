from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from bot.db import repo
from bot.db.models import AttemptStatus
from bot.services import phases
from bot.services.game import GameService
from bot.services.results import render_header, render_review
from tests.factory import TOTAL, Clock, FakeMessenger, make_content, register_users, set_running
from tests.test_game import Env, btn_for, play_all

U = 101
NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


@pytest.fixture
async def env(session_factory):
    await register_users(session_factory, *range(101, 110))
    await set_running(session_factory)
    clock, messenger = Clock(), FakeMessenger()
    game = GameService(session_factory, make_content(), messenger, clock=clock, rng=random.Random(1))
    yield Env(game, messenger, clock, session_factory)
    game.scheduler.cancel_all()


# --- фініш (§6.6) --------------------------------------------------------


async def test_finish_sends_result_promo_partners_and_feedback_prompt(env):
    await play_all(env)
    texts = [e[2] for e in env.messenger.of("text", U)]
    m = env.game.content.messages
    tail = texts[-3:]
    assert f"<b>{TOTAL} з {TOTAL}</b>" in tail[0]
    assert tail[1] == env.game.content.meta.promo.text and "HILLEL1000" in tail[1]
    assert tail[2] == m["partners"]
    (prompt,) = env.messenger.of("feedback_prompt", U)
    assert prompt[2] == m["ask_feedback"]
    a = await env.attempt()
    assert a.promo_sent is True and a.feedback is None


async def test_promo_is_sent_only_once_even_if_finish_runs_twice(env):
    await play_all(env)
    a = await env.attempt()
    env.messenger.clear()
    await env.game._finish(a)  # повторний виклик не дублює промокод
    promo = env.game.content.meta.promo.text
    assert promo not in [e[2] for e in env.messenger.of("text", U)]


# --- закриття (§6.7) ------------------------------------------------------


async def test_close_quest_marks_unfinished_and_notifies_with_promo(env):
    await env.start_and_go(U)  # у питанні 1
    a = await env.attempt()
    await env.game.answer(U, 0, btn_for(env.game, a, True))  # 1 відповідь, бал 1
    await env.game.start_or_resume(U + 1)  # ще на картці
    await play_all(env, U + 2)  # завершив

    async with env.sf.begin() as s:
        closed = await phases.close_quest(s, now=NOW)
    assert {a.tg_id for a in closed} == {U, U + 1}  # завершений не чіпаємо
    state = None
    async with env.sf() as s:
        state = await repo.get_game_state(s)
    assert state.phase == "CLOSED" and state.registration_open is False and state.closed_at == NOW

    env.game.cancel_timers(closed)
    items = await env.game.close_notifications(closed)
    by_user: dict[int, list[str]] = {}
    for tg_id, text in items:
        by_user.setdefault(tg_id, []).append(text)
    assert "Зараховано відповідей: 1 з 5, балів: 1" in by_user[U][0]
    assert "Зараховано відповідей: 0 з 5, балів: 0" in by_user[U + 1][0]
    for uid in (U, U + 1):  # FR-19: промокод отримує й той, хто не встиг
        assert env.game.content.meta.promo.text in by_user[uid]

    # таймер закритої спроби більше не спрацює
    assert closed[0].id not in env.game.scheduler
    # повторно промокод не надсилається
    assert [t for _, t in await env.game.close_notifications(closed) if "HILLEL1000" in t] == []


async def test_close_quest_requires_running_phase(env):
    async with env.sf.begin() as s:
        await repo.update_game_state(s, phase="REGISTRATION")
    async with env.sf.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="лише під час гри"):
            await phases.close_quest(s, now=NOW)


async def test_close_in_sets_planned_time_only_while_running(env):
    async with env.sf.begin() as s:
        state = await phases.set_closes_in(s, minutes=15, now=NOW)
    assert state.closes_at == NOW + timedelta(minutes=15) and state.phase == "RUNNING"  # сам не закриває
    async with env.sf.begin() as s:
        await repo.update_game_state(s, phase="CLOSED")
    async with env.sf.begin() as s:
        with pytest.raises(phases.PhaseConflict):
            await phases.set_closes_in(s, minutes=5, now=NOW)


async def test_publish_results_only_after_close_and_only_once(env):
    async with env.sf.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="/close_quest"):
            await phases.publish_results(s)
        await repo.update_game_state(s, phase="CLOSED")
    async with env.sf.begin() as s:
        assert (await phases.publish_results(s)).phase == "RESULTS"
    async with env.sf.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="вже опубліковано"):
            await phases.publish_results(s)


# --- рейтинг (FR-15, FR-16, FR-22) ---------------------------------------


async def make_finished(env, uid, score, total_ms, finished_at, status="FINISHED"):
    """Ставить спробі точні результати напряму: так тайбрейки перевіряються без годинника."""
    await register_users(env.sf, uid)
    async with env.sf.begin() as s:
        a = await repo.create_attempt(s, uid, [{"qid": "q1", "option_order": [0, 1]}], NOW)
        from sqlalchemy import update

        from bot.db.models import Attempt

        await s.execute(
            update(Attempt)
            .where(Attempt.id == a.id)
            .values(status=status, score=score, total_time_ms=total_ms, finished_at=finished_at)
        )


async def ranking(env):
    async with env.sf() as s:
        return [(r.tg_id, r.place) for r in await repo.leaderboard(s)]


async def test_more_points_wins(env):
    await make_finished(env, 1, 3, 1000, NOW)
    await make_finished(env, 2, 5, 9000, NOW)
    assert await ranking(env) == [(2, 1), (1, 2)]


async def test_equal_points_less_total_time_wins(env):
    await make_finished(env, 1, 4, 30_000, NOW)
    await make_finished(env, 2, 4, 20_000, NOW + timedelta(minutes=5))
    assert await ranking(env) == [(2, 1), (1, 2)]


async def test_equal_points_and_time_earlier_finish_wins(env):
    await make_finished(env, 1, 4, 20_000, NOW + timedelta(minutes=2))
    await make_finished(env, 2, 4, 20_000, NOW + timedelta(minutes=1))
    assert await ranking(env) == [(2, 1), (1, 2)]


async def test_unfinished_attempts_are_ranked_and_lose_ties_on_finish_time(env):
    await make_finished(env, 1, 3, 20_000, None, status="CLOSED_UNFINISHED")
    await make_finished(env, 2, 3, 20_000, NOW, status="FINISHED")
    await make_finished(env, 3, 9, 1, None, status="CLOSED_UNFINISHED")  # FR-22: теж у рейтингу
    assert await ranking(env) == [(3, 1), (2, 2), (1, 3)]


async def test_admins_excluded_and_unfinished_in_progress_are_not_ranked(env):
    await make_finished(env, 1, 5, 1, NOW)
    await make_finished(env, 2, 5, 1, NOW)
    await make_finished(env, 3, 5, 1, NOW)
    await make_finished(env, 4, 5, 1, NOW, status="QUESTION")  # ще грає
    async with env.sf.begin() as s:
        await repo.set_user_flag(s, 2, excluded=True)  # FR-16
        await repo.set_user_flag(s, 3, is_admin=True)
    assert await ranking(env) == [(1, 1)]


# --- розбір результатів (§6.8) --------------------------------------------


async def test_review_shows_marks_correct_answers_and_explanations(env):
    content = env.game.content
    content.rounds[0].sections[0].questions[0].explanation = "Бо <<b>> так"  # має екрануватись
    await play_all(env, U, correct=False)
    a = await env.attempt()
    async with env.sf() as s:
        answers = await repo.answers_by_attempt(s, [a.id])
    chunks = render_review(env.game.index, a, answers[a.id])
    review = "\n\n".join(chunks)

    assert review.count("❌") == TOTAL  # усі відповіді хибні
    assert "Правильна відповідь: b" in review  # q1: правильний варіант «b»
    assert "Правильна відповідь: Так" in review  # бінарні: підпис мітки
    assert "💡 Бо &lt;&lt;b&gt;&gt; так" in review
    assert [ln for ln in review.splitlines() if ln.startswith("<b>1.</b>")]


async def test_review_marks_timeouts_and_unanswered(env):
    await env.start_and_go()
    a = await env.attempt()
    await env.game.on_timeout(a.id, 0)  # таймаут на першому
    async with env.sf.begin() as s:
        await repo.close_unfinished(s)  # решта не відповідана
    a = await env.attempt()
    async with env.sf() as s:
        answers = await repo.answers_by_attempt(s, [a.id])
    review = "\n\n".join(render_review(env.game.index, a, answers[a.id]))
    assert review.count("⏰") == 1 and review.count("➖") == TOTAL - 1


async def test_review_is_split_into_messages_under_telegram_limit(env):
    content = env.game.content
    for rnd in content.rounds:  # довгі пояснення, щоб розбір не вмістився в одне повідомлення
        for section in rnd.sections:
            for q in section.questions:
                q.explanation = "пояснення " * 120
    await play_all(env, U)
    a = await env.attempt()
    async with env.sf() as s:
        answers = await repo.answers_by_attempt(s, [a.id])
    chunks = render_review(env.game.index, a, answers[a.id])
    assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks).count("<b>") == TOTAL  # жодне питання не загубилось


async def test_review_with_real_content_fits_limits_and_is_valid_html():
    from bot.config import PROJECT_ROOT
    from bot.content.loader import load_content
    from bot.content.textutil import html_problems
    from bot.services.plan import ContentIndex, build_plan

    real = load_content(PROJECT_ROOT / "content" / "quest.yaml")
    index = ContentIndex(real)
    plan = build_plan(real, random.Random(0))

    class A:  # лише те, що читає render_review
        pass

    attempt = A()
    attempt.plan = plan
    chunks = render_review(index, attempt, {})
    assert all(len(c) <= 4096 for c in chunks)
    for chunk in chunks:
        assert html_problems(chunk) == []


def test_result_header_uses_message_template():
    content = make_content()
    text = render_header(content, 12, 143, 41, 55)
    assert "<b>12</b> з 143" in text and "<b>41 з 55</b>" in text
