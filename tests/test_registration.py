from __future__ import annotations

import pytest
from aiogram.methods import AnswerCallbackQuery

from bot.db import repo
from bot.db.models import Phase
from bot.handlers.registration import clean_name
from bot.keyboards import START_GAME_DATA, CourseCb, StatusCb
from tests.harness import ADMIN_ID, Harness

U = 5001  # звичайний учасник
CODE = "Secret"


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def set_state(session_factory, phase=Phase.REGISTRATION, open_=True, code=CODE):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
        await repo.update_game_state(s, phase=phase.value, registration_open=open_, access_code=code)


async def db_user(session_factory, tg_id=U):
    async with session_factory() as s:
        return await repo.get_user(s, tg_id)


async def register(h: Harness, user=U, name="Іван Петренко", status="FIT_STUDENT", course="2"):
    await h.send(user, "/start")
    await h.send(user, CODE)
    await h.send(user, name)
    await h.press(user, StatusCb(value=status).pack())
    if course:
        await h.press(user, CourseCb(value=course).pack())


# --- clean_name ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Іван Петренко", "Іван Петренко"),
        ("  Іван    Петренко  ", "Іван Петренко"),
        ("Іван https://spam.com/x Петренко", "Іван Петренко"),
        ("t.me/scam Оля", "Оля"),
        ("@Іван", "Іван"),
        ("а" * 80, "а" * 60),
        ("a", None),
        ("@@ x ", None),
        ("https://spam.com", None),
        ("", None),
    ],
)
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


# --- доступ -------------------------------------------------------------


async def test_registration_closed_in_draft(h, session_factory):
    await set_state(session_factory, Phase.DRAFT, open_=False)
    await h.send(U, "/start")
    assert h.texts(U) == [h.content.messages["registration_closed"]]


async def test_closed_flag_blocks_even_during_registration(h, session_factory):
    await set_state(session_factory, Phase.REGISTRATION, open_=False)
    await h.send(U, "/start")
    assert h.texts(U) == [h.content.messages["registration_closed"]]


async def test_empty_access_code_never_matches(h, session_factory):
    await set_state(session_factory, code="")
    await h.send(U, "/start")
    await h.send(U, "   ")
    await h.send(U, "")
    assert h.texts(U)[-1] == h.content.messages["wrong_code"]
    assert (await db_user(session_factory)).registered_at is None


async def test_wrong_then_right_code_case_insensitive_trimmed(h, session_factory):
    await set_state(session_factory)
    m = h.content.messages
    await h.send(U, "/start")
    await h.send(U, "nope")
    await h.send(U, "  sEcReT ")
    assert h.texts(U) == [m["welcome"], m["wrong_code"], m["consent_note"], m["ask_name"]]


async def test_code_attempt_limit(h, session_factory):
    await set_state(session_factory)
    # 4 хибні + 1 вірна = 5 спроб за хвилину: ще дозволено
    await h.send(U, "/start")
    for _ in range(4):
        await h.send(U, "wrong")
    await h.send(U, CODE)
    assert h.last_text(U) == h.content.messages["ask_name"]

    # 5 хибних + 6-та (навіть вірна) — відхилена
    await h.send(U + 1, "/start")
    for _ in range(5):
        await h.send(U + 1, "wrong")
    await h.send(U + 1, CODE)
    assert h.last_text(U + 1) == h.content.messages["too_many_attempts"]


async def test_deep_link_with_right_code_skips_code_step(h, session_factory):
    await set_state(session_factory)
    m = h.content.messages
    await h.send(U, f"/start {CODE}")
    assert h.texts(U) == [m["consent_note"], m["ask_name"]]


async def test_deep_link_with_wrong_code(h, session_factory):
    await set_state(session_factory)
    await h.send(U, "/start bad")
    assert h.texts(U) == [h.content.messages["wrong_code"]]
    await h.send(U, CODE)  # можна ввести код вручну
    assert h.last_text(U) == h.content.messages["ask_name"]


# --- повний сценарій ----------------------------------------------------


async def test_full_flow_fit_student(h, session_factory):
    await set_state(session_factory)
    await register(h)
    m = h.content.messages
    assert h.texts(U) == [
        m["welcome"],
        m["consent_note"],
        m["ask_name"],
        m["ask_status"],
        m["ask_course"],
        m["registered_waiting"],
    ]
    user = await db_user(session_factory)
    assert (user.full_name, user.status, user.course) == ("Іван Петренко", "FIT_STUDENT", "2")
    assert user.registered_at is not None


async def test_status_keyboard_offers_all_statuses(h, session_factory):
    await set_state(session_factory)
    await h.send(U, f"/start {CODE}")
    await h.send(U, "Іван Петренко")
    assert [StatusCb.unpack(d).value for d in h.button_data(U)] == [
        "FIT_STUDENT",
        "APPLICANT",
        "OTHER_STUDENT",
        "OTHER",
    ]


async def test_non_fit_skips_course(h, session_factory):
    await set_state(session_factory)
    await register(h, status="APPLICANT", course=None)
    assert h.last_text(U) == h.content.messages["registered_waiting"]
    assert h.content.messages["ask_course"] not in h.texts(U)
    user = await db_user(session_factory)
    assert (user.status, user.course) == ("APPLICANT", None)


async def test_invalid_name_is_reasked(h, session_factory):
    await set_state(session_factory)
    await h.send(U, f"/start {CODE}")
    await h.send(U, "а")
    await h.send(U, "https://spam.com")
    assert h.texts(U)[-2:] == [h.content.messages["invalid_name"]] * 2
    await h.send(U, "Оля @ https://x.y Коваль")
    await h.press(U, StatusCb(value="OTHER").pack())
    assert (await db_user(session_factory)).full_name == "Оля Коваль"


async def test_text_instead_of_button_repeats_question(h, session_factory):
    await set_state(session_factory)
    await h.send(U, f"/start {CODE}")
    await h.send(U, "Іван Петренко")
    await h.send(U, "не знаю")
    assert h.last_text(U) == h.content.messages["ask_status"]
    assert h.button_data(U)  # клавіатура знову є


async def test_registration_closed_while_filling_in(h, session_factory):
    await set_state(session_factory)
    await h.send(U, f"/start {CODE}")
    await h.send(U, "Іван Петренко")
    await set_state(session_factory, open_=False)
    await h.press(U, StatusCb(value="OTHER").pack())
    assert h.last_text(U) == h.content.messages["registration_closed"]
    assert (await db_user(session_factory)).registered_at is None


async def test_stale_callbacks_without_state_are_ignored(h, session_factory):
    await set_state(session_factory)
    await h.send(U, "/start")  # стан = очікуємо код
    h.clear()
    await h.press(U, StatusCb(value="OTHER").pack())  # кроку «статус» ще не було
    await h.press(U, CourseCb(value="1").pack())
    assert h.sent(U) == []
    assert (await db_user(session_factory)).registered_at is None


async def test_double_press_does_not_register_twice(h, session_factory):
    await set_state(session_factory)
    await register(h)
    first = (await db_user(session_factory)).registered_at
    h.clear()
    await h.press(U, CourseCb(value="4").pack())  # дубль кнопки: стан уже очищено
    assert h.sent(U) == []
    user = await db_user(session_factory)
    assert user.registered_at == first and user.course == "2"


# --- після реєстрації ---------------------------------------------------


async def test_registered_user_start_shows_waiting_then_start_button(h, session_factory):
    await set_state(session_factory)
    await register(h)
    m = h.content.messages

    h.clear()
    await h.send(U, "/start")
    assert h.texts(U) == [m["registered_waiting"]]

    await set_state(session_factory, Phase.RUNNING)
    h.clear()
    await h.send(U, "/start")
    assert h.texts(U) == [m["quest_started"]]
    assert h.button_data(U) == [START_GAME_DATA]


async def test_registering_during_running_gets_start_button(h, session_factory):
    await set_state(session_factory, Phase.RUNNING)
    await register(h)
    assert h.last_text(U) == h.content.messages["quest_started"]
    assert h.button_data(U) == [START_GAME_DATA]


async def test_registered_user_cannot_restart_registration(h, session_factory):
    await set_state(session_factory)
    await register(h)
    h.clear()
    await h.send(U, f"/start {CODE}")
    await h.send(U, "якийсь текст")
    assert h.texts(U) == [h.content.messages["registered_waiting"]]


async def test_start_button_is_a_stub_alert(h, session_factory):
    await set_state(session_factory, Phase.RUNNING)
    await register(h)
    h.clear()
    await h.press(U, START_GAME_DATA)
    (answer,) = [c for c in h.session.calls if isinstance(c, AnswerCallbackQuery)]
    assert answer.text == h.content.messages["game_not_ready"] and answer.show_alert


# --- текст без стану та middleware --------------------------------------


async def test_text_without_state_restarts_registration(h, session_factory):
    """Після рестарту бота FSM порожній: учасник повертається на початок."""
    await set_state(session_factory)
    await h.send(U, "привіт")
    assert h.texts(U) == [h.content.messages["welcome"]]
    await h.send(U, CODE)
    assert h.last_text(U) == h.content.messages["ask_name"]


async def test_unknown_commands_get_no_reply(h, session_factory):
    await set_state(session_factory)
    await h.send(U, "/whatever")
    assert h.sent(U) == []


async def test_user_upsert_refreshes_telegram_data_and_flags_admin(h, session_factory):
    await set_state(session_factory)
    await h.send(U, "/start", username="first")
    await h.send(U, "щось", username="second")
    await h.send(ADMIN_ID, "/start")
    user = await db_user(session_factory)
    assert (user.username, user.is_admin) == ("second", False)
    assert (await db_user(session_factory, ADMIN_ID)).is_admin is True
