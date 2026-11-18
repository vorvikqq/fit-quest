from __future__ import annotations

import pytest

from bot import texts
from bot.db import repo
from bot.db.models import Phase
from bot.keyboards import START_GAME_DATA, AdminAct, Confirm
from tests.factory import make_todo_content
from tests.harness import ADMIN_ID, Harness

U = 5001


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory, content=make_todo_content())  # 2 незаповнені питання


async def game(session_factory):
    async with session_factory() as s:
        return await repo.get_game_state(s)


async def set_state(session_factory, **fields):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s, "CODE")
        await repo.update_game_state(s, **fields)


async def add_registered(session_factory, *ids, blocked=()):
    from datetime import UTC, datetime

    async with session_factory.begin() as s:
        for uid in ids:
            await repo.upsert_user(s, uid, f"u{uid}", "n")
            await repo.register_user(s, uid, "Ім Я", "OTHER", None, datetime.now(UTC))
        for uid in blocked:
            await repo.set_user_flag(s, uid, blocked_bot=True)


def yes(action, force=False):
    return Confirm(action=action, force=force, yes=True).pack()


def no(action, force=False):
    return Confirm(action=action, force=force, yes=False).pack()


# --- доступ -------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["/admin", "/setcode X1", "/open_reg", "/close_reg", "/start_quest"])
async def test_non_admin_commands_are_silently_ignored(h, session_factory, cmd):
    await set_state(session_factory)
    await h.send(U, cmd)
    assert h.sent(U) == []
    st = await game(session_factory)
    assert (st.phase, st.registration_open, st.access_code) == (Phase.DRAFT, False, "CODE")


async def test_non_admin_cannot_press_admin_buttons(h, session_factory):
    await set_state(session_factory)
    await h.press(U, yes("open_reg"))
    await h.press(U, AdminAct(action="open_reg").pack())
    assert h.sent(U) == [] and h.edits() == []
    assert (await game(session_factory)).phase == Phase.DRAFT


# --- /admin -------------------------------------------------------------


async def test_admin_panel_shows_state_and_counters(h, session_factory):
    await set_state(session_factory)
    await add_registered(session_factory, 1, 2, 3)
    await h.send(ADMIN_ID, "/admin")
    panel = h.last_text(ADMIN_ID)
    assert "DRAFT" in panel and "закрита" in panel
    assert "<code>CODE</code>" in panel
    assert "Зареєстровано: <b>3</b>" in panel
    assert "TODO: 2" in panel and "5 питань" in panel
    assert set(h.button_data(ADMIN_ID)) == {
        AdminAct(action=a).pack() for a in ("open_reg", "close_reg", "start_quest", "refresh")
    }


async def test_admin_panel_warns_when_code_empty(h, session_factory):
    await set_state(session_factory, access_code="")
    await h.send(ADMIN_ID, "/admin")
    assert "не задано" in h.last_text(ADMIN_ID)


async def test_panel_refresh_edits_message(h, session_factory):
    await set_state(session_factory)
    await h.press(ADMIN_ID, AdminAct(action="refresh").pack())
    assert len(h.edits()) == 1 and "Панель адміна" in h.edits()[0].text


async def test_panel_button_asks_for_confirmation(h, session_factory):
    await set_state(session_factory)
    await h.press(ADMIN_ID, AdminAct(action="open_reg").pack())
    assert h.last_text(ADMIN_ID) == "<b>Відкрити реєстрацію?</b>\nКодове слово: <code>CODE</code>"
    assert set(h.button_data(ADMIN_ID)) == {yes("open_reg"), no("open_reg")}


# --- /setcode -----------------------------------------------------------


async def test_setcode_without_argument_shows_usage(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/setcode")
    assert h.last_text(ADMIN_ID) == texts.ADMIN_USAGE_SETCODE
    assert (await game(session_factory)).access_code == "CODE"


async def test_setcode_changes_code_used_for_registration(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.send(ADMIN_ID, "/setcode  Нове Слово ")
    assert (await game(session_factory)).access_code == "Нове Слово"

    await h.send(U, "/start")
    await h.send(U, "CODE")  # старий більше не працює
    assert h.last_text(U) == h.content.messages["wrong_code"]
    await h.send(U, "нове слово")
    assert h.last_text(U) == h.content.messages["ask_name"]


async def test_setcode_escapes_html_in_reply(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/setcode <b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in h.last_text(ADMIN_ID)


# --- /open_reg, /close_reg ----------------------------------------------


async def test_open_reg_requires_confirmation_then_applies(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/open_reg")
    assert (await game(session_factory)).registration_open is False  # ще нічого не змінено
    assert set(h.button_data(ADMIN_ID)) == {yes("open_reg"), no("open_reg")}

    await h.press(ADMIN_ID, yes("open_reg"))
    st = await game(session_factory)
    assert (st.phase, st.registration_open) == (Phase.REGISTRATION, True)
    assert h.edits()[-1].text == texts.done("open_reg", 0)
    assert h.edits()[-1].reply_markup is None  # кнопки зникли


async def test_cancel_changes_nothing(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/open_reg")
    await h.press(ADMIN_ID, no("open_reg"))
    assert (await game(session_factory)).phase == Phase.DRAFT
    assert h.edits()[-1].text == texts.CANCELLED


async def test_open_reg_when_already_open_is_refused_without_buttons(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.send(ADMIN_ID, "/open_reg")
    assert "вже відкрита" in h.last_text(ADMIN_ID)
    assert h.button_data(ADMIN_ID) == []


async def test_close_reg_keeps_phase(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.send(ADMIN_ID, "/close_reg")
    await h.press(ADMIN_ID, yes("close_reg"))
    st = await game(session_factory)
    assert (st.phase, st.registration_open) == (Phase.REGISTRATION, False)

    await h.send(U, "/start")
    assert h.last_text(U) == h.content.messages["registration_closed"]


async def test_confirm_is_rechecked_when_state_changed(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/open_reg")
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.press(ADMIN_ID, yes("open_reg"))  # хтось уже відкрив
    assert "вже відкрита" in h.edits()[-1].text


# --- /start_quest -------------------------------------------------------


async def test_start_quest_refused_before_registration(h, session_factory):
    await set_state(session_factory)
    await h.send(ADMIN_ID, "/start_quest force")
    assert "/open_reg" in h.last_text(ADMIN_ID)
    assert h.button_data(ADMIN_ID) == []


async def test_start_quest_blocked_by_todo(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.send(ADMIN_ID, "/start_quest")
    assert "2 незаповнених" in h.last_text(ADMIN_ID)
    assert h.button_data(ADMIN_ID) == []
    assert (await game(session_factory)).phase == Phase.REGISTRATION


async def test_start_quest_rejects_unknown_argument(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value)
    await h.send(ADMIN_ID, "/start_quest please")
    assert h.last_text(ADMIN_ID) == texts.ADMIN_USAGE_START_QUEST


async def test_forced_start_broadcasts_to_registered_only(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await add_registered(session_factory, 11, 12, 13, blocked=[13])
    async with session_factory.begin() as s:
        await repo.upsert_user(s, 14, "x", "незареєстрований")

    await h.send(ADMIN_ID, "/start_quest force")
    confirm_text = h.last_text(ADMIN_ID)
    assert "Зареєстровано: 3" in confirm_text and "Примусовий старт" in confirm_text
    h.clear()

    await h.press(ADMIN_ID, yes("start_quest", force=True))
    await h.wait_background()

    st = await game(session_factory)
    assert st.phase == Phase.RUNNING and st.content_hash == "hash-123" and st.started_at
    assert st.registration_open is True  # FR-25: реєстрація лишається відкритою

    started = h.content.messages["quest_started"]
    assert h.texts(11) == [started] and h.texts(12) == [started]
    assert h.texts(13) == [] and h.texts(14) == []  # заблокований і незареєстрований не отримують
    assert h.button_data(11) == [START_GAME_DATA]
    assert "надіслано 2 з 2" in h.last_text(ADMIN_ID)


async def test_double_confirm_starts_once(h, session_factory):
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await add_registered(session_factory, 11)
    await h.send(ADMIN_ID, "/start_quest force")
    h.clear()

    await h.press(ADMIN_ID, yes("start_quest", force=True))
    await h.press(ADMIN_ID, yes("start_quest", force=True))  # повторне натискання
    await h.wait_background()

    assert "уже стартував" in h.edits()[-1].text
    assert h.texts(11) == [h.content.messages["quest_started"]]  # розсилка лише одна


async def test_forced_flag_cannot_be_forged_via_plain_confirm(h, session_factory):
    """✅ без force (наприклад, зі старої кнопки) не обходить блокування TODO."""
    await set_state(session_factory, phase=Phase.REGISTRATION.value, registration_open=True)
    await h.press(ADMIN_ID, yes("start_quest", force=False))
    assert "незаповнених" in h.edits()[-1].text
    assert (await game(session_factory)).phase == Phase.REGISTRATION
