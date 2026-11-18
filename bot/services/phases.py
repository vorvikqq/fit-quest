"""Глобальний етап квесту (DESIGN §3.1, FR-24, FR-25).

```
DRAFT ─open_reg─▶ REGISTRATION ─start_quest─▶ RUNNING ─close_quest─▶ CLOSED ─publish─▶ RESULTS
```
`registration_open` — окремий прапорець: реєстрацію можна відкривати й закривати
незалежно від етапу (доки квест не закрито).

Правила переходів — у чистих `check_*`: їх викликає і попередня перевірка (щоб адмін
не бачив кнопки «підтвердити» для неможливої дії), і сама зміна під `FOR UPDATE`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import GameState, Phase

Action = Literal["open_reg", "close_reg", "start_quest"]


class PhaseConflict(Exception):
    """Дія неможлива в поточному стані. Текст призначений для адміна."""


def registration_allowed(state: GameState) -> bool:
    return state.registration_open and state.phase in (Phase.REGISTRATION, Phase.RUNNING)


def check_open_registration(state: GameState) -> None:
    if state.phase in (Phase.CLOSED, Phase.RESULTS):
        raise PhaseConflict("Квест уже закрито, реєстрацію відкрити не можна.")
    if state.registration_open:
        raise PhaseConflict("Реєстрація вже відкрита.")


def check_close_registration(state: GameState) -> None:
    if not state.registration_open:
        raise PhaseConflict("Реєстрація вже закрита.")


def check_start_quest(state: GameState, todo_count: int, force: bool) -> None:
    if state.phase == Phase.RUNNING:
        raise PhaseConflict("Квест уже стартував.")
    if state.phase == Phase.DRAFT:
        raise PhaseConflict("Реєстрацію ще не відкрито. Спочатку /open_reg.")
    if state.phase != Phase.REGISTRATION:
        raise PhaseConflict(f"Старт неможливий з етапу {state.phase}.")
    if todo_count and not force:
        raise PhaseConflict(
            f"У контенті {todo_count} незаповнених питань (TODO), старт заблоковано (FR-30). "
            "Для тестового прогону: /start_quest force."
        )


def preflight(action: Action, state: GameState, *, todo_count: int = 0, force: bool = False) -> None:
    """Перевірка без змін: кидає `PhaseConflict`, якщо дія зараз неможлива."""
    if action == "open_reg":
        check_open_registration(state)
    elif action == "close_reg":
        check_close_registration(state)
    else:
        check_start_quest(state, todo_count, force)


async def open_registration(session: AsyncSession) -> GameState:
    state = await repo.get_game_state(session, lock=True)
    check_open_registration(state)
    values: dict = {"registration_open": True}
    if state.phase == Phase.DRAFT:
        values["phase"] = Phase.REGISTRATION.value
    return await repo.update_game_state(session, **values)


async def close_registration(session: AsyncSession) -> GameState:
    state = await repo.get_game_state(session, lock=True)
    check_close_registration(state)
    return await repo.update_game_state(session, registration_open=False)


async def start_quest(
    session: AsyncSession, *, content_hash: str, now: datetime, todo_count: int, force: bool
) -> GameState:
    state = await repo.get_game_state(session, lock=True)
    check_start_quest(state, todo_count, force)
    return await repo.update_game_state(
        session, phase=Phase.RUNNING.value, started_at=now, content_hash=content_hash
    )
