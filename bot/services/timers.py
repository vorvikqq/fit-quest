"""Таймери питань, сторож і відновлення після рестарту (DESIGN §7).

Сервер — джерело правди: asyncio-таймер лише «будильник». Якщо він загубиться,
сторож (раз на 5 с) і recovery при старті закриють питання самі.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING

from bot.db import repo

if TYPE_CHECKING:
    from bot.services.game import GameService

log = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 5.0  # с
WATCHDOG_GRACE = timedelta(seconds=3)  # дедлайн минув більш ніж на стільки, а таймер мовчить
RECOVERY_CONCURRENCY = 20


class TimerScheduler:
    """`dict[attempt_id] → TimerHandle` (через `loop.call_later`)."""

    def __init__(self) -> None:
        self._handles: dict[int, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, key: int, delay: float, callback: Callable[[], Awaitable[object]]) -> None:
        """Планує виклик; повторний виклик для того ж ключа замінює попередній таймер."""
        self.cancel(key)
        loop = asyncio.get_running_loop()
        self._handles[key] = loop.call_later(max(delay, 0.0), self._fire, key, callback)

    def cancel(self, key: int) -> None:
        handle = self._handles.pop(key, None)
        if handle is not None:
            handle.cancel()

    def cancel_all(self) -> None:
        for key in list(self._handles):
            self.cancel(key)

    def __contains__(self, key: int) -> bool:
        return key in self._handles

    def __len__(self) -> int:
        return len(self._handles)

    def _fire(self, key: int, callback: Callable[[], Awaitable[object]]) -> None:
        self._handles.pop(key, None)
        task = asyncio.get_running_loop().create_task(callback())
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Таймер впав", exc_info=task.exception())

    async def drain(self) -> None:
        """Чекає завершення вже запущених колбеків (для тестів і акуратного завершення)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def recover(game: GameService) -> None:
    """Recovery при старті: перепланувати живі таймери, закрити прострочені, дослати питання."""
    async with game.session_factory() as session:
        active = list(await repo.list_active_questions(session))
        unsent = list(await repo.list_unsent_questions(session))
    now = game.clock()
    log.info("Recovery: питань у грі %d, не надісланих %d", len(active), len(unsent))

    gate = asyncio.Semaphore(RECOVERY_CONCURRENCY)

    async def guarded(coro: Awaitable[object]) -> None:
        async with gate:
            try:
                await coro
            except Exception:  # noqa: BLE001
                log.exception("Recovery: збій для однієї спроби")

    jobs: list[Awaitable[object]] = []
    for attempt in active:
        if attempt.q_deadline + game.grace <= now:
            jobs.append(guarded(game.on_timeout(attempt.id, attempt.cursor)))
        else:
            game.arm_timer(attempt)
    jobs.extend(guarded(game.send_current_question(a)) for a in unsent)
    await asyncio.gather(*jobs)


async def sweep(game: GameService) -> int:
    """Один прохід сторожа: закриває питання, які прострочені, а таймер не спрацював."""
    async with game.session_factory() as session:
        stale = list(await repo.list_stale_questions(session, game.clock(), WATCHDOG_GRACE))
    closed = 0
    for attempt in stale:
        if await game.on_timeout(attempt.id, attempt.cursor):
            closed += 1
    if closed:
        log.warning("Сторож закрив %d прострочених питань (таймер не спрацював)", closed)
    return closed


async def run_watchdog(game: GameService, interval: float = WATCHDOG_INTERVAL) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep(game)
        except Exception:  # noqa: BLE001
            log.exception("Сторож: збій проходу")
