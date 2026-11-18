from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from bot.db import repo
from bot.db.models import GameState, Phase
from bot.services import phases

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


async def current(session_factory) -> GameState:
    async with session_factory() as s:
        return await repo.get_game_state(s)


@pytest.fixture
async def fresh(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s, "CODE")
    return session_factory


async def force_state(session_factory, **fields):
    async with session_factory.begin() as s:
        await repo.update_game_state(s, **fields)


def state(phase: Phase, open_: bool) -> GameState:
    return GameState(id=1, phase=phase.value, registration_open=open_, access_code="x")


# --- registration_allowed -----------------------------------------------


@pytest.mark.parametrize(
    ("phase", "open_", "allowed"),
    [
        (Phase.DRAFT, True, False),  # прапорець без етапу не працює
        (Phase.REGISTRATION, True, True),
        (Phase.REGISTRATION, False, False),
        (Phase.RUNNING, True, True),  # FR-25: реєстрація під час гри
        (Phase.RUNNING, False, False),
        (Phase.CLOSED, True, False),
        (Phase.RESULTS, True, False),
    ],
)
def test_registration_allowed(phase, open_, allowed):
    assert phases.registration_allowed(state(phase, open_)) is allowed


# --- open / close registration ------------------------------------------


async def test_open_registration_from_draft_moves_to_registration(fresh):
    async with fresh.begin() as s:
        await phases.open_registration(s)
    st = await current(fresh)
    assert (st.phase, st.registration_open) == (Phase.REGISTRATION, True)


async def test_open_registration_twice_is_conflict(fresh):
    async with fresh.begin() as s:
        await phases.open_registration(s)
    async with fresh.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="вже відкрита"):
            await phases.open_registration(s)


async def test_close_then_reopen_keeps_phase(fresh):
    async with fresh.begin() as s:
        await phases.open_registration(s)
    async with fresh.begin() as s:
        await phases.close_registration(s)
    st = await current(fresh)
    assert (st.phase, st.registration_open) == (Phase.REGISTRATION, False)

    await force_state(fresh, phase=Phase.RUNNING.value)
    async with fresh.begin() as s:
        await phases.open_registration(s)  # у RUNNING етап не чіпаємо
    st = await current(fresh)
    assert (st.phase, st.registration_open) == (Phase.RUNNING, True)


async def test_close_registration_when_closed_is_conflict(fresh):
    async with fresh.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="вже закрита"):
            await phases.close_registration(s)


@pytest.mark.parametrize("phase", [Phase.CLOSED, Phase.RESULTS])
async def test_cannot_open_registration_after_quest_closed(fresh, phase):
    await force_state(fresh, phase=phase.value)
    async with fresh.begin() as s:
        with pytest.raises(phases.PhaseConflict, match="закрито"):
            await phases.open_registration(s)


# --- start_quest --------------------------------------------------------


async def start(session_factory, todo_count=0, force=False):
    async with session_factory.begin() as s:
        return await phases.start_quest(
            s, content_hash="abc", now=NOW, todo_count=todo_count, force=force
        )


async def test_start_requires_registration_phase(fresh):
    with pytest.raises(phases.PhaseConflict, match="/open_reg"):
        await start(fresh)  # DRAFT
    assert (await current(fresh)).phase == Phase.DRAFT


async def test_start_blocked_by_todo_unless_forced(fresh):
    await force_state(fresh, phase=Phase.REGISTRATION.value)
    with pytest.raises(phases.PhaseConflict, match="16 незаповнених"):
        await start(fresh, todo_count=16)
    assert (await current(fresh)).phase == Phase.REGISTRATION

    st = await start(fresh, todo_count=16, force=True)
    assert st.phase == Phase.RUNNING


async def test_start_records_time_and_content_hash(fresh):
    await force_state(fresh, phase=Phase.REGISTRATION.value)
    st = await start(fresh)
    assert (st.phase, st.started_at, st.content_hash) == (Phase.RUNNING, NOW, "abc")


async def test_start_does_not_touch_registration_flag(fresh):
    await force_state(fresh, phase=Phase.REGISTRATION.value, registration_open=True)
    assert (await start(fresh)).registration_open is True


async def test_start_twice_is_conflict(fresh):
    await force_state(fresh, phase=Phase.REGISTRATION.value)
    await start(fresh)
    with pytest.raises(phases.PhaseConflict, match="уже стартував"):
        await start(fresh)


async def test_concurrent_starts_succeed_once(fresh):
    await force_state(fresh, phase=Phase.REGISTRATION.value)
    results = await asyncio.gather(*(start(fresh) for _ in range(8)), return_exceptions=True)
    assert sum(isinstance(r, GameState) for r in results) == 1
    assert sum(isinstance(r, phases.PhaseConflict) for r in results) == 7


def test_preflight_matches_executors():
    with pytest.raises(phases.PhaseConflict):
        phases.preflight("start_quest", state(Phase.DRAFT, False))
    phases.preflight("start_quest", state(Phase.REGISTRATION, True))
    phases.preflight("open_reg", state(Phase.DRAFT, False))
    with pytest.raises(phases.PhaseConflict):
        phases.preflight("close_reg", state(Phase.DRAFT, False))
