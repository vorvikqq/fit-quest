from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import timedelta

import pytest

from bot.db import repo
from bot.db.models import Attempt, AttemptStatus
from bot.services.game import GameService
from bot.services.timers import recover, sweep
from tests.factory import TOTAL, Clock, FakeMessenger, make_content, register_users, set_running

U = 101


@dataclass
class Env:
    game: GameService
    messenger: FakeMessenger
    clock: Clock
    sf: object

    async def attempt(self, uid: int = U) -> Attempt | None:
        async with self.sf() as s:
            return await repo.get_attempt_for_user(s, uid)

    async def answers(self, uid: int = U):
        a = await self.attempt(uid)
        async with self.sf() as s:
            return list(await repo.list_answers(s, a.id))

    def new_game(self, **kw) -> GameService:
        """«Перезапуск бота»: новий сервіс над тією ж БД, без таймерів у пам'яті."""
        return GameService(self.sf, make_content(), self.messenger, clock=self.clock, **kw)

    async def btn(self, correct: bool, uid: int = U) -> int:
        a = await self.attempt(uid)
        return btn_for(self.game, a, correct)

    async def start_and_go(self, uid: int = U) -> Attempt:
        await self.game.start_or_resume(uid)
        a = await self.attempt(uid)
        assert await self.game.go(uid, a.cursor)
        return await self.attempt(uid)


def btn_for(game: GameService, attempt: Attempt, correct: bool) -> int:
    entry = attempt.plan[attempt.cursor]
    q = game.index.info(entry["qid"]).question
    order = entry["option_order"]
    if correct:
        return order.index(q.correct)
    return next(pos for pos, orig in enumerate(order) if orig != q.correct)


@pytest.fixture
async def env(session_factory):
    await register_users(session_factory, U, U + 1, U + 2)
    await set_running(session_factory)
    clock, messenger = Clock(), FakeMessenger()
    game = GameService(
        session_factory, make_content(), messenger, clock=clock, rng=random.Random(42)
    )
    yield Env(game, messenger, clock, session_factory)
    game.scheduler.cancel_all()


async def play_all(env: Env, uid: int = U, correct: bool = True, tick: float = 2) -> None:
    """Проходить усю гру: картки → «Почати раунд» → відповіді."""
    await env.game.start_or_resume(uid)
    for _ in range(50):
        a = await env.attempt(uid)
        if a.status == AttemptStatus.ROUND_INTRO:
            assert await env.game.go(uid, a.cursor)
        elif a.status == AttemptStatus.QUESTION:
            env.clock.advance(tick)
            assert await env.game.answer(uid, a.cursor, btn_for(env.game, a, correct))
        else:
            return
    raise AssertionError("гра не завершилась")


# --- старт --------------------------------------------------------------


async def test_start_creates_attempt_sends_rules_and_first_card(env):
    assert await env.game.start_or_resume(U) == "started"
    a = await env.attempt()
    assert (a.status, a.cursor, a.score) == (AttemptStatus.ROUND_INTRO, 0, 0)
    assert len(a.plan) == TOTAL

    texts = env.messenger.of("text", U)
    assert texts[0][2] == env.game.content.messages["rules"]
    (card,) = env.messenger.cards(U)
    assert "<b>Раунд Один</b>" in card[2] and card[3] == 0
    assert env.messenger.questions(U) == []  # час на картці не рахується: питання ще немає


@pytest.mark.parametrize(
    ("phase", "result"),
    [("DRAFT", "not_started"), ("REGISTRATION", "not_started"), ("CLOSED", "closed"), ("RESULTS", "closed")],
)
async def test_cannot_start_when_quest_not_running(env, phase, result):
    async with env.sf.begin() as s:
        await repo.update_game_state(s, phase=phase)
    assert await env.game.start_or_resume(U) == result
    assert await env.attempt() is None


async def test_unregistered_cannot_start(env):
    async with env.sf.begin() as s:
        await repo.upsert_user(s, 999, None, "x")
    assert await env.game.start_or_resume(999) == "not_registered"
    assert await env.game.start_or_resume(12345) == "not_registered"  # навіть невідомий акаунт


async def test_start_shows_time_left_when_closing_planned(env):
    async with env.sf.begin() as s:
        await repo.update_game_state(s, closes_at=env.clock.now + timedelta(minutes=15))
    await env.game.start_or_resume(U)
    assert any("15 хв" in e[2] for e in env.messenger.of("text", U))


async def test_second_start_resumes_instead_of_creating_new_attempt(env):
    await env.game.start_or_resume(U)
    first = await env.attempt()
    assert await env.game.start_or_resume(U) == "resumed"
    again = await env.attempt()
    assert again.id == first.id and again.plan == first.plan
    assert len(env.messenger.cards(U)) == 2  # картку показали знову


async def test_parallel_starts_create_one_attempt(env):
    results = await asyncio.gather(*(env.game.start_or_resume(U) for _ in range(6)))
    assert results.count("started") == 1
    async with env.sf() as s:
        from sqlalchemy import func, select

        assert (await s.execute(select(func.count()).select_from(Attempt))).scalar_one() == 1


# --- питання ------------------------------------------------------------


async def test_go_sends_question_and_records_deadline(env):
    a = await env.start_and_go()
    assert a.status == AttemptStatus.QUESTION
    assert a.q_sent_at == env.clock.now
    assert a.q_deadline == env.clock.now + timedelta(seconds=10)
    assert a.q_message_id is not None
    (_, _, view, cursor) = env.messenger.questions(U)[0]
    assert cursor == 0 and view.text.startswith("Раунд 1/2 · Питання 1/2 · ⏱ 10 с")
    assert a.id in env.game.scheduler  # таймер-будильник заведено


async def test_double_go_sends_only_one_question(env):
    await env.game.start_or_resume(U)
    results = await asyncio.gather(*(env.game.go(U, 0) for _ in range(8)))
    assert results.count(True) == 1
    assert len(env.messenger.questions(U)) == 1


async def test_stale_go_is_ignored(env):
    await env.start_and_go()
    assert not await env.game.go(U, 0)  # питання вже йде
    assert not await env.game.go(U, 3)  # чужий курсор
    assert not await env.game.go(555, 0)  # немає спроби
    assert len(env.messenger.questions(U)) == 1


# --- відповіді ----------------------------------------------------------


async def test_correct_answer_scores_and_sends_next_question(env):
    a = await env.start_and_go()
    env.clock.advance(3)
    assert await env.game.answer(U, 0, btn_for(env.game, a, correct=True))

    a = await env.attempt()
    assert (a.cursor, a.score, a.total_time_ms, a.status) == (1, 1, 3000, AttemptStatus.QUESTION)
    (answer,) = await env.answers()
    assert (answer.qid, answer.is_correct, answer.response_ms) == (a.plan[0]["qid"], True, 3000)
    assert env.messenger.of("close", U)[0][3] == env.game.content.messages["answer_accepted"]
    assert len(env.messenger.questions(U)) == 2 and env.messenger.questions(U)[1][3] == 1


async def test_wrong_answer_scores_zero(env):
    a = await env.start_and_go()
    assert await env.game.answer(U, 0, btn_for(env.game, a, correct=False))
    a = await env.attempt()
    assert a.score == 0
    (answer,) = await env.answers()
    assert answer.is_correct is False and answer.chosen is not None


async def test_chosen_is_stored_in_original_order_despite_shuffle(env):
    """Кнопка → індекс в ОРИГІНАЛЬНОМУ порядку з yaml → правильність (DESIGN §6.4)."""
    for uid in range(200, 220):  # у кожного свій порядок варіантів
        await register_users(env.sf, uid)
        a = await env.start_and_go(uid)
        order = a.plan[0]["option_order"]
        btn = btn_for(env.game, a, correct=True)
        assert order[btn] == 1  # правильний варіант q1 — оригінальний індекс 1
        assert await env.game.answer(uid, 0, btn)
        (answer,) = await env.answers(uid)
        assert answer.chosen == 1 and answer.is_correct
    shown = {tuple((await env.attempt(u)).plan[0]["option_order"]) for u in range(200, 220)}
    assert len(shown) > 3


async def test_response_time_is_capped_by_limit(env):
    a = await env.start_and_go()
    env.clock.advance(11)  # 1 с понад ліміт 10 с, але в межах допуску 1,5 с
    assert await env.game.answer(U, 0, btn_for(env.game, a, correct=True))
    a = await env.attempt()
    (answer,) = await env.answers()
    assert answer.is_correct and answer.response_ms == 10_000 and a.total_time_ms == 10_000


async def test_answer_after_grace_counts_as_timeout(env):
    a = await env.start_and_go()
    env.clock.advance(11.6)  # > 10 + 1,5
    assert await env.game.answer(U, 0, btn_for(env.game, a, correct=True))
    a = await env.attempt()
    (answer,) = await env.answers()
    assert (answer.chosen, answer.is_correct, answer.response_ms) == (None, False, 10_000)
    assert a.score == 0 and a.total_time_ms == 10_000
    assert env.messenger.of("close", U)[0][3] == env.game.content.messages["time_is_up"]


async def test_stale_duplicate_and_invalid_answers_are_rejected(env):
    a = await env.start_and_go()
    good = btn_for(env.game, a, correct=True)
    assert not await env.game.answer(U, 0, 9)  # такої кнопки немає
    assert not await env.game.answer(U, 1, good)  # чужий курсор
    assert not await env.game.answer(777, 0, good)  # немає спроби
    assert (await env.attempt()).cursor == 0  # нічого не зачепило

    assert await env.game.answer(U, 0, good)
    assert not await env.game.answer(U, 0, good)  # подвійне натискання
    assert (await env.attempt()).score == 1 and len(await env.answers()) == 1


async def test_cannot_answer_while_round_card_is_shown(env):
    await env.game.start_or_resume(U)
    assert not await env.game.answer(U, 0, 0)


async def test_two_simultaneous_answers_count_once(env):
    """Головна вимога: дві одночасні відповіді на одне питання → одне зарахування."""
    a = await env.start_and_go()
    good = btn_for(env.game, a, correct=True)
    bad = btn_for(env.game, a, correct=False)

    results = await asyncio.gather(env.game.answer(U, 0, good), env.game.answer(U, 0, bad))
    assert sorted(results) == [False, True]

    a = await env.attempt()
    assert a.cursor == 1
    assert len(await env.answers()) == 1
    assert a.score == (await env.answers())[0].is_correct  # зараховано рівно одну з двох
    assert len(env.messenger.questions(U)) == 2  # наступне питання надіслано один раз


async def test_many_simultaneous_answers_count_once(env):
    a = await env.start_and_go()
    good = btn_for(env.game, a, correct=True)
    results = await asyncio.gather(*(env.game.answer(U, 0, good) for _ in range(25)))
    assert results.count(True) == 1
    a = await env.attempt()
    assert (a.cursor, a.score) == (1, 1) and len(await env.answers()) == 1
    assert len(env.messenger.of("close", U)) == 1 and len(env.messenger.questions(U)) == 2


async def test_answer_and_timeout_race_counts_once(env):
    a = await env.start_and_go()
    good = btn_for(env.game, a, correct=True)
    results = await asyncio.gather(env.game.answer(U, 0, good), env.game.on_timeout(a.id, 0))
    assert sorted(results) == [False, True]
    assert (await env.attempt()).cursor == 1 and len(await env.answers()) == 1


# --- таймаут ------------------------------------------------------------


async def test_timeout_records_zero_and_moves_on(env):
    a = await env.start_and_go()
    assert await env.game.on_timeout(a.id, 0)
    a = await env.attempt()
    (answer,) = await env.answers()
    assert (answer.chosen, answer.is_correct, answer.response_ms) == (None, False, 10_000)
    assert (a.cursor, a.score, a.total_time_ms) == (1, 0, 10_000)
    assert env.messenger.of("close", U)[0][3] == env.game.content.messages["time_is_up"]
    assert len(env.messenger.questions(U)) == 2  # далі бот надіслав наступне питання


async def test_timeout_is_idempotent_and_ignores_stale_cursor(env):
    a = await env.start_and_go()
    assert not await env.game.on_timeout(a.id, 5)
    assert await env.game.on_timeout(a.id, 0)
    assert not await env.game.on_timeout(a.id, 0)  # вдруге вже нічого
    assert (await env.attempt()).cursor == 1 and len(await env.answers()) == 1


async def test_timeout_does_nothing_on_round_card(env):
    await env.game.start_or_resume(U)
    a = await env.attempt()
    assert not await env.game.on_timeout(a.id, 0)


async def test_real_timer_fires_at_deadline_plus_grace(session_factory):
    """Справжній asyncio-таймер: ліміт 1 с + допуск 0,2 с."""
    from datetime import UTC, datetime

    await register_users(session_factory, U)
    await set_running(session_factory)
    rounds = [
        {
            "id": "r1",
            "title": "R",
            "time_limit": 1,
            "sections": [
                {
                    "id": "s1",
                    "question_type": "binary",
                    "labels": ["a", "b"],
                    "questions": [
                        {"id": "q1", "text": "1", "correct": 0},
                        {"id": "q2", "text": "2", "correct": 0},
                    ],
                }
            ],
        }
    ]
    messenger = FakeMessenger()
    game = GameService(
        session_factory,
        make_content(rounds),
        messenger,
        clock=lambda: datetime.now(UTC),
        grace=timedelta(milliseconds=200),
    )
    await game.start_or_resume(U)
    assert await game.go(U, 0)

    await asyncio.sleep(0.6)
    async with session_factory() as s:
        assert (await repo.get_attempt_for_user(s, U)).cursor == 0  # ще в межах ліміту
    await asyncio.sleep(1.0)
    await game.scheduler.drain()
    async with session_factory() as s:
        a = await repo.get_attempt_for_user(s, U)
    assert a.cursor == 1 and a.status == AttemptStatus.QUESTION
    assert len(messenger.questions(U)) == 2  # таймер сам видав наступне питання
    game.scheduler.cancel_all()


# --- проходження ---------------------------------------------------------


async def test_full_playthrough_with_cards_between_sections(env):
    await env.game.start_or_resume(U)
    await play_all(env)  # (start_or_resume всередині повертає "resumed" — це нормально)

    a = await env.attempt()
    assert (a.status, a.cursor, a.score) == (AttemptStatus.FINISHED, TOTAL, TOTAL)
    assert a.finished_at is not None and a.total_time_ms == TOTAL * 2000
    assert len(await env.answers()) == TOTAL

    # картки: старт раунду 1 (cursor 0), раунду 2 (2), секції 2b (4)
    assert [c[3] for c in env.messenger.cards(U)][-3:] == [0, 2, 4]
    cards = {c[3]: c[2] for c in env.messenger.cards(U)}
    assert "Раунд Два" in cards[2] and "вступ секції 2a" in cards[2]
    assert "Раунд Два" not in cards[4] and "вступ секції 2b" in cards[4]
    # питань надіслано рівно стільки, скільки в плані
    assert [q[3] for q in env.messenger.questions(U)] == list(range(TOTAL))

    assert any(f"<b>{TOTAL} з {TOTAL}</b>" in e[2] for e in env.messenger.of("text", U))


async def test_playthrough_with_wrong_answers_scores_zero(env):
    await play_all(env, correct=False)
    a = await env.attempt()
    assert a.status == AttemptStatus.FINISHED and a.score == 0


async def test_closing_message_does_not_reveal_correctness(env):
    """FR-12: правильна й неправильна відповідь отримують однакове підтвердження."""
    await play_all(env, U, correct=True)
    await play_all(env, U + 1, correct=False)
    right = {e[3] for e in env.messenger.of("close", U)}
    wrong = {e[3] for e in env.messenger.of("close", U + 1)}
    assert right == wrong == {env.game.content.messages["answer_accepted"]}


# --- відновлення, сторож, повернення ------------------------------------


async def test_recovery_times_out_expired_question_and_sends_next(env):
    a = await env.start_and_go()
    env.clock.advance(60)  # бот «лежав»: дедлайн давно минув
    game2 = env.new_game()
    await recover(game2)
    after = await env.attempt()
    assert after.cursor == 1 and (await env.answers())[0].chosen is None
    assert len(env.messenger.questions(U)) == 2
    game2.scheduler.cancel_all()


async def test_recovery_rearms_timer_for_live_question(env):
    a = await env.start_and_go()
    env.clock.advance(4)
    game2 = env.new_game()
    await recover(game2)
    assert a.id in game2.scheduler  # будильник перепланований
    assert (await env.attempt()).cursor == 0  # питання ще живе
    assert len(env.messenger.questions(U)) == 1  # нічого зайвого не надіслано
    game2.scheduler.cancel_all()


async def test_recovery_resends_question_that_was_never_delivered(env):
    """Бот упав між прийомом відповіді й надсиланням наступного питання."""
    a = await env.start_and_go()
    async with env.sf.begin() as s:  # імітуємо: відповідь прийнята, наступне не надіслане
        await repo.advance_attempt(s, a.id, 0, AttemptStatus.QUESTION)
    game2 = env.new_game()
    await recover(game2)
    after = await env.attempt()
    assert after.q_sent_at is not None and after.cursor == 1
    assert [q[3] for q in env.messenger.questions(U)] == [0, 1]
    game2.scheduler.cancel_all()


async def test_recovery_handles_many_attempts(env):
    ids = list(range(300, 330))
    await register_users(env.sf, *ids)
    for uid in ids:
        await env.start_and_go(uid)
    env.clock.advance(120)
    game2 = env.new_game()
    await recover(game2)
    for uid in ids:
        assert (await env.attempt(uid)).cursor == 1
    game2.scheduler.cancel_all()


async def test_watchdog_closes_question_when_timer_was_lost(env):
    a = await env.start_and_go()
    env.game.scheduler.cancel_all()  # таймер «загубився»
    env.clock.advance(12)  # дедлайн минув на 2 с: ще в межах пільгових 3 с
    assert await sweep(env.game) == 0
    env.clock.advance(1.5)  # тепер минуло > 3 с
    assert await sweep(env.game) == 1
    assert (await env.attempt()).cursor == 1
    assert await sweep(env.game) == 0  # повторний прохід нічого не чіпає


async def test_start_resends_current_question_with_same_deadline(env):
    a = await env.start_and_go()
    old_message = a.q_message_id
    env.clock.advance(4)
    env.messenger.clear()

    assert await env.game.start_or_resume(U) == "resumed"
    after = await env.attempt()
    assert after.q_deadline == a.q_deadline and after.q_sent_at == a.q_sent_at
    assert after.q_message_id != old_message  # id нового повідомлення
    assert len(env.messenger.questions(U)) == 1
    assert ("remove_kb", U, old_message) in env.messenger.events  # стару клавіатуру прибрано


async def test_resume_on_finished_and_closed_attempts(env):
    await play_all(env)
    env.messenger.clear()
    assert await env.game.start_or_resume(U) == "resumed"
    assert f"{TOTAL} з {TOTAL}" in env.messenger.of("text", U)[-1][2]

    await env.start_and_go(U + 1)
    async with env.sf.begin() as s:
        await repo.close_unfinished(s)
    env.messenger.clear()
    await env.game.start_or_resume(U + 1)
    assert "Квест закрито" in env.messenger.of("text", U + 1)[-1][2]


async def test_answers_rejected_after_quest_closed(env):
    a = await env.start_and_go()
    good = btn_for(env.game, a, correct=True)
    async with env.sf.begin() as s:
        await repo.close_unfinished(s)
    assert not await env.game.answer(U, 0, good)
    assert not await env.game.on_timeout(a.id, 0)
    assert (await env.attempt()).status == AttemptStatus.CLOSED_UNFINISHED


# --- користувач заблокував бота -----------------------------------------


async def test_blocked_user_still_gets_to_the_end_with_zeros(env):
    env.messenger.blocked.add(U)  # Telegram нічого не доставляє: message_id = None
    await env.game.start_or_resume(U)
    for _ in range(30):
        a = await env.attempt()
        if a.status == AttemptStatus.FINISHED:
            break
        if a.status == AttemptStatus.ROUND_INTRO:
            await env.game.go(U, a.cursor)
        else:
            assert await env.game.on_timeout(a.id, a.cursor)  # таймери самі закривають питання
    a = await env.attempt()
    assert a.status == AttemptStatus.FINISHED and a.score == 0
    assert len(await env.answers()) == TOTAL


# --- незалежність користувачів ------------------------------------------


async def test_concurrent_players_do_not_interfere(env):
    ids = list(range(400, 440))
    await register_users(env.sf, *ids)
    await asyncio.gather(*(play_all(env, uid, correct=(uid % 2 == 0), tick=0) for uid in ids))  # спільний годинник: не крутимо
    for uid in ids:
        a = await env.attempt(uid)
        assert a.status == AttemptStatus.FINISHED
        assert a.score == (TOTAL if uid % 2 == 0 else 0)
        assert len(await env.answers(uid)) == TOTAL
