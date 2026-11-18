from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import AnswerCallbackQuery, SendMessage

from bot.db import repo
from bot.keyboards import START_GAME_DATA
from bot.middlewares import AdminOnlyMiddleware, ThrottlingMiddleware
from bot.services.broadcast import Broadcaster
from bot.services.ratelimit import SlidingWindowLimiter
from tests.harness import Harness

# --- SlidingWindowLimiter -----------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_limiter_allows_limit_then_blocks_then_recovers():
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 60, clock)
    assert [limiter.hit(1) for _ in range(4)] == [True, True, True, False]
    assert limiter.hit(2)  # інший ключ незалежний
    clock.t += 59
    assert not limiter.hit(1)
    clock.t += 2  # вікно минуло
    assert limiter.hit(1)


def test_limiter_blocked_hits_do_not_extend_the_window():
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 10, clock)
    assert limiter.hit(1)
    for _ in range(5):
        clock.t += 1
        assert not limiter.hit(1)
    clock.t += 5  # 10 с від першої успішної події
    assert limiter.hit(1)


def test_limiter_reset():
    limiter = SlidingWindowLimiter(1, 60, Clock())
    limiter.hit(1)
    limiter.reset(1)
    assert limiter.hit(1)


# --- middleware ---------------------------------------------------------


class FakeUser:
    def __init__(self, id: int) -> None:
        self.id = id


async def run_middleware(mw, data):
    calls = []

    async def handler(event, d):
        calls.append(event)
        return "handled"

    result = await mw(handler, object(), data)
    return result, calls


async def test_throttling_drops_fast_repeats_per_user():
    clock = Clock()
    mw = ThrottlingMiddleware(0.5, clock)
    data = {"event_from_user": FakeUser(1)}

    assert (await run_middleware(mw, data))[1] != []
    clock.t += 0.3
    assert (await run_middleware(mw, data))[1] == []  # відкинуто
    assert (await run_middleware(mw, {"event_from_user": FakeUser(2)}))[1] != []  # інший юзер
    clock.t += 0.3  # від останнього ПРИЙНЯТОГО вже 0.6 с
    assert (await run_middleware(mw, data))[1] != []


async def test_throttling_ignores_events_without_user():
    result, calls = await run_middleware(ThrottlingMiddleware(0.5, Clock()), {})
    assert result == "handled" and len(calls) == 1


async def test_admin_only_blocks_non_admin():
    mw = AdminOnlyMiddleware()
    assert await run_middleware(mw, {"is_admin": False}) == (None, [])
    assert await run_middleware(mw, {}) == (None, [])
    result, calls = await run_middleware(mw, {"is_admin": True})
    assert result == "handled" and len(calls) == 1


async def test_throttling_end_to_end_answers_dropped_button(session_factory):
    h = Harness(session_factory, throttle_interval=60)
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    await h.press(7, START_GAME_DATA)
    await h.press(7, START_GAME_DATA)  # відкинуто, але спінер знято
    answers = [c for c in h.session.calls if isinstance(c, AnswerCallbackQuery)]
    assert len(answers) == 2
    assert answers[1].text is None  # друге натискання: тиха відповідь без обробки


# --- Broadcaster --------------------------------------------------------


class FakeBot:
    """Імітує bot.send_message з наперед заданими збоями."""

    def __init__(self, failures: dict[int, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.sent: list[int] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        queue = self.failures.get(chat_id)
        if queue:
            raise queue.pop(0)
        self.sent.append(chat_id)


def method() -> SendMessage:
    return SendMessage(chat_id=1, text="x")


def make_broadcaster(bot, session_factory, rate=25.0):
    clock = Clock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.t += seconds

    return Broadcaster(bot, session_factory, rate=rate, clock=clock, sleep=sleep), sleeps


async def test_broadcast_spaces_messages_by_rate(session_factory):
    bot = FakeBot()
    broadcaster, sleeps = make_broadcaster(bot, session_factory, rate=25)
    result = await broadcaster.send(range(1, 6), "hi")
    assert (result.sent, result.total) == (5, 5) and bot.sent == [1, 2, 3, 4, 5]
    # 5 повідомлень = 4 паузи по 1/25 с: ≤ 25 повідомлень/с
    assert sleeps == pytest.approx([0.04] * 4)


async def test_broadcast_retries_on_retry_after(session_factory):
    bot = FakeBot({2: [TelegramRetryAfter(method(), "flood", 3)]})
    broadcaster, sleeps = make_broadcaster(bot, session_factory)
    result = await broadcaster.send([1, 2, 3], "hi")
    assert (result.sent, result.failed) == (3, 0)
    assert 3 in sleeps  # дочекались retry_after
    assert bot.sent == [1, 2, 3]


async def test_broadcast_gives_up_after_max_retries(session_factory):
    flood = [TelegramRetryAfter(method(), "flood", 1) for _ in range(10)]
    broadcaster, _ = make_broadcaster(FakeBot({1: flood}), session_factory)
    result = await broadcaster.send([1, 2], "hi")
    assert (result.sent, result.failed) == (1, 1)


async def test_broadcast_marks_blocked_users(session_factory):
    async with session_factory.begin() as s:
        for uid in (1, 2):
            await repo.upsert_user(s, uid, None, "n")
    bot = FakeBot({1: [TelegramForbiddenError(method(), "bot was blocked by the user")]})
    broadcaster, _ = make_broadcaster(bot, session_factory)
    result = await broadcaster.send([1, 2], "hi")
    assert (result.sent, result.blocked, result.failed) == (1, 1, 0)
    async with session_factory() as s:
        assert (await repo.get_user(s, 1)).blocked_bot is True
        assert (await repo.get_user(s, 2)).blocked_bot is False


async def test_broadcast_other_errors_do_not_stop_the_run(session_factory):
    bot = FakeBot({1: [TelegramServerError(method(), "boom")]})
    broadcaster, _ = make_broadcaster(bot, session_factory)
    result = await broadcaster.send([1, 2, 3], "hi")
    assert (result.sent, result.failed) == (2, 1)
