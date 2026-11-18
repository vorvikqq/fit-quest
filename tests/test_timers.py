from __future__ import annotations

import asyncio

from bot.services.timers import TimerScheduler


async def test_timer_fires_after_delay():
    scheduler, fired = TimerScheduler(), []

    async def cb():
        fired.append(1)

    scheduler.schedule(1, 0.05, cb)
    assert 1 in scheduler and len(scheduler) == 1
    await asyncio.sleep(0.01)
    assert fired == []
    await asyncio.sleep(0.1)
    await scheduler.drain()
    assert fired == [1] and 1 not in scheduler


async def test_cancel_prevents_firing():
    scheduler, fired = TimerScheduler(), []

    async def cb():
        fired.append(1)

    scheduler.schedule(1, 0.05, cb)
    scheduler.cancel(1)
    scheduler.cancel(1)  # повторне скасування безпечне
    await asyncio.sleep(0.12)
    assert fired == [] and len(scheduler) == 0


async def test_rescheduling_replaces_previous_timer():
    scheduler, fired = TimerScheduler(), []

    async def first():
        fired.append("first")

    async def second():
        fired.append("second")

    scheduler.schedule(1, 0.05, first)
    scheduler.schedule(1, 0.05, second)
    await asyncio.sleep(0.12)
    await scheduler.drain()
    assert fired == ["second"]


async def test_timers_are_independent_per_key_and_cancel_all():
    scheduler, fired = TimerScheduler(), []

    def make(n):
        async def cb():
            fired.append(n)

        return cb

    for n in range(5):
        scheduler.schedule(n, 0.05, make(n))
    scheduler.cancel(2)
    await asyncio.sleep(0.12)
    await scheduler.drain()
    assert sorted(fired) == [0, 1, 3, 4]

    scheduler.schedule(9, 0.05, make(9))
    scheduler.cancel_all()
    await asyncio.sleep(0.1)
    assert 9 not in fired


async def test_failing_callback_does_not_break_scheduler(caplog):
    scheduler, fired = TimerScheduler(), []

    async def boom():
        raise RuntimeError("збій")

    async def ok():
        fired.append(1)

    scheduler.schedule(1, 0.01, boom)
    scheduler.schedule(2, 0.03, ok)
    await asyncio.sleep(0.1)
    await scheduler.drain()
    assert fired == [1]


async def test_past_deadline_fires_immediately():
    scheduler, fired = TimerScheduler(), []

    async def cb():
        fired.append(1)

    scheduler.schedule(1, -5, cb)
    await asyncio.sleep(0.03)
    await scheduler.drain()
    assert fired == [1]
