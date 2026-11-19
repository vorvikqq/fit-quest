from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe

from bot.__main__ import connect_telegram


class FakeBot:
    def __init__(self, errors: list[Exception]) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "me"


def reset() -> TelegramNetworkError:
    return TelegramNetworkError(GetMe(), "ClientConnectorError: Connection reset by peer")


async def test_connects_immediately_when_telegram_is_reachable():
    bot = FakeBot([])
    assert await connect_telegram(bot) == "me" and bot.calls == 1


async def test_retries_after_connection_reset_with_growing_pause():
    """Те, що сталося в логу: перше з'єднання скинуто, друге вже проходить."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    bot = FakeBot([reset(), reset()])
    assert await connect_telegram(bot, sleep=fake_sleep) == "me"
    assert bot.calls == 3 and sleeps == [2.0, 4.0]


async def test_gives_up_after_all_attempts():
    async def no_sleep(seconds: float) -> None:
        pass

    bot = FakeBot([reset() for _ in range(10)])
    with pytest.raises(TelegramNetworkError):
        await connect_telegram(bot, attempts=3, sleep=no_sleep)
    assert bot.calls == 3


async def test_wrong_token_is_not_retried():
    bot = FakeBot([TelegramUnauthorizedError(GetMe(), "Unauthorized")])
    with pytest.raises(TelegramUnauthorizedError):
        await connect_telegram(bot)
    assert bot.calls == 1


async def test_pause_is_capped():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    bot = FakeBot([reset() for _ in range(9)])
    await connect_telegram(bot, attempts=10, sleep=fake_sleep)
    assert max(sleeps) == 15.0
