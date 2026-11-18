"""Повний життєвий цикл через Telegram-хендлери: реєстрація → старт → гра → закриття → результати."""

from __future__ import annotations

import pytest

from bot import texts
from bot.db import repo
from bot.db.models import AttemptStatus, Phase
from bot.keyboards import START_GAME_DATA, AnsCb, Confirm, GoCb, StatusCb
from tests.factory import TOTAL, make_content
from tests.harness import ADMIN_ID, Harness

U1, U2, U3 = 6001, 6002, 6003
CODE = "Secret"


@pytest.fixture
async def h(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    return Harness(session_factory, content=make_content())


def yes(action, force=False, arg=0):
    return Confirm(action=action, force=force, arg=arg, yes=True).pack()


def no(action, force=False, arg=0):
    return Confirm(action=action, force=force, arg=arg, yes=False).pack()


async def game(session_factory):
    async with session_factory() as s:
        return await repo.get_game_state(s)


async def attempt(session_factory, uid):
    async with session_factory() as s:
        return await repo.get_attempt_for_user(s, uid)


async def register(h, uid):
    await h.send(uid, f"/start {CODE}")
    await h.send(uid, f"Учасник {uid}")
    await h.press(uid, StatusCb(value="OTHER").pack())


async def play(h, session_factory, uid, answers: int | None = None, correct=True):
    """Тисне «Почати квест» і відповідає на `answers` питань (None = до кінця)."""
    await h.press(uid, START_GAME_DATA)
    done = 0
    for _ in range(40):
        a = await attempt(session_factory, uid)
        if a.status == AttemptStatus.ROUND_INTRO:
            if answers is not None and done >= answers:
                return
            await h.press(uid, GoCb(cursor=a.cursor).pack())
            a = await attempt(session_factory, uid)
        if a.status != AttemptStatus.QUESTION:
            return
        if answers is not None and done >= answers:
            return
        entry = a.plan[a.cursor]
        q = h.game.index.info(entry["qid"]).question
        order = entry["option_order"]
        btn = order.index(q.correct) if correct else next(i for i, o in enumerate(order) if o != q.correct)
        await h.press(uid, AnsCb(cursor=a.cursor, btn=btn).pack())
        done += 1


async def open_and_start(h, session_factory, users=(U1, U2, U3)):
    await h.send(ADMIN_ID, f"/setcode {CODE}")
    await h.send(ADMIN_ID, "/open_reg")
    await h.press(ADMIN_ID, yes("open_reg"))
    for uid in users:
        await register(h, uid)
    await h.send(ADMIN_ID, "/start_quest")
    await h.press(ADMIN_ID, yes("start_quest"))
    await h.wait_background()


# --- повний сценарій -----------------------------------------------------


async def test_whole_event_from_registration_to_published_results(h, session_factory):
    await open_and_start(h, session_factory)
    started = h.content.messages["quest_started"]
    assert all(started in h.texts(u) for u in (U1, U2, U3))

    await play(h, session_factory, U1)  # усе правильно
    await play(h, session_factory, U2, answers=3, correct=True)  # застряг на 4-му питанні
    # U3 так і не натиснув «Почати квест»

    h.clear()
    await h.send(ADMIN_ID, "/close_in 10")
    await h.press(ADMIN_ID, yes("close_in", arg=10))
    await h.wait_background()
    soon = h.content.messages["closing_soon"].format(minutes=10)
    assert all(h.texts(u) == [soon] for u in (U1, U2, U3))
    assert (await game(session_factory)).closes_at is not None

    h.clear()
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    st = await game(session_factory)
    assert st.phase == Phase.CLOSED and st.registration_open is False
    # закриту спробу бачить лише U2: U1 завершив, а U3 і не починав
    assert h.texts(U1) == [] and h.texts(U3) == []
    closed_text = h.texts(U2)[0]
    assert "Зараховано відповідей: 3 з 5, балів: 3" in closed_text
    assert h.content.meta.promo.text in h.texts(U2)  # FR-19
    assert (await attempt(session_factory, U2)).status == AttemptStatus.CLOSED_UNFINISHED
    assert "надіслано" in h.last_text(ADMIN_ID)

    # після закриття відповісти/почати/зареєструватись уже не можна
    h.clear()
    await h.press(U3, START_GAME_DATA)
    assert h.answers()[-1].text == h.content.messages["quest_closed_late"]
    await h.send(U3 + 100, f"/start {CODE}")
    assert h.last_text(U3 + 100) == h.content.messages["registration_closed"]

    h.clear()
    await h.send(ADMIN_ID, "/publish_results")
    await h.press(ADMIN_ID, yes("publish_results"))
    await h.wait_background()
    assert (await game(session_factory)).phase == Phase.RESULTS

    header1, header2 = h.texts(U1)[0], h.texts(U2)[0]
    assert "<b>1</b> з 2" in header1 and "<b>5 з 5</b>" in header1  # U1: перше місце
    assert "<b>2</b> з 2" in header2 and "<b>3 з 5</b>" in header2
    assert h.texts(U3) == []  # без спроби — не в рейтингу
    assert "✅" in h.texts(U1)[1] and "➖" in h.texts(U2)[1]  # розбір; у U2 неотримані питання
    async with session_factory() as s:
        assert all(a.results_sent for a in [await repo.get_attempt_for_user(s, U1)])


async def test_admin_cannot_skip_stages(h, session_factory):
    await h.send(ADMIN_ID, "/close_quest")
    assert "лише під час гри" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/publish_results")
    assert "/close_quest" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/close_in 5")
    assert "лише під час гри" in h.last_text(ADMIN_ID)


@pytest.mark.parametrize("bad", ["", "abc", "0", "601", "-5", "1.5"])
async def test_close_in_validates_minutes(h, session_factory, bad):
    await h.send(ADMIN_ID, f"/close_in {bad}".strip())
    assert h.last_text(ADMIN_ID) == texts.ADMIN_USAGE_CLOSE_IN


async def test_close_quest_and_publish_confirm_can_be_cancelled_and_not_replayed(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, no("close_quest"))
    assert (await game(session_factory)).phase == Phase.RUNNING

    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    h.clear()
    await h.press(ADMIN_ID, yes("close_quest"))  # застаріла кнопка після закриття
    assert "лише під час гри" in h.edits()[-1].text
    await h.wait_background()
    assert h.texts(U1) == []  # повторного оповіщення нема


async def test_close_quest_confirmation_warns_how_many_are_playing(h, session_factory):
    await open_and_start(h, session_factory, users=(U1, U2))
    await play(h, session_factory, U1, answers=1)
    h.clear()
    await h.send(ADMIN_ID, "/close_quest")
    assert "Зараз грають: 1" in h.last_text(ADMIN_ID)


# --- /broadcast ----------------------------------------------------------


async def test_broadcast_previews_then_sends_to_registered_only(h, session_factory):
    await open_and_start(h, session_factory, users=(U1, U2))
    await h.send(U3, "/start")  # незареєстрований
    h.clear()

    await h.send(ADMIN_ID, "/broadcast Лишилось 2 хвилини! <b>Поспішайте</b> & не панікуйте")
    preview = h.last_text(ADMIN_ID)
    assert "Отримувачів: 2" in preview
    assert "&lt;b&gt;Поспішайте&lt;/b&gt; &amp; не панікуйте" in preview  # введений вручну HTML екранується
    assert h.texts(U1) == []  # поки лише попередній перегляд

    await h.press(ADMIN_ID, yes("broadcast"))
    await h.wait_background()
    expected = "Лишилось 2 хвилини! &lt;b&gt;Поспішайте&lt;/b&gt; &amp; не панікуйте"
    assert h.texts(U1) == [expected] and h.texts(U2) == [expected]
    assert h.texts(U3) == []
    assert "надіслано 2 з 2" in h.last_text(ADMIN_ID)


async def test_broadcast_cancel_and_usage_and_lost_text(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await h.send(ADMIN_ID, "/broadcast")
    assert h.last_text(ADMIN_ID) == texts.ADMIN_USAGE_BROADCAST

    await h.send(ADMIN_ID, "/broadcast привіт")
    await h.press(ADMIN_ID, no("broadcast"))
    await h.press(ADMIN_ID, yes("broadcast"))  # текст уже забуто
    assert texts.NOTHING_TO_BROADCAST in h.edits()[-1].text
    assert h.texts(U1)[-1] == h.content.messages["quest_started"]  # нічого нового не надіслано


async def test_broadcast_skips_users_who_blocked_the_bot(h, session_factory):
    await open_and_start(h, session_factory, users=(U1, U2))
    async with session_factory.begin() as s:
        await repo.set_user_flag(s, U2, blocked_bot=True)
    h.clear()
    await h.send(ADMIN_ID, "/broadcast hi")
    await h.press(ADMIN_ID, yes("broadcast"))
    await h.wait_background()
    assert h.texts(U1) == ["hi"] and h.texts(U2) == []


async def test_non_admin_cannot_broadcast_or_close(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    h.clear()
    for cmd in ("/broadcast hi", "/close_quest", "/publish_results", "/close_in 5"):
        await h.send(U1, cmd)
    await h.press(U1, yes("close_quest"))
    assert h.sent(U1) == []
    assert (await game(session_factory)).phase == Phase.RUNNING


# --- відгук (§6.6, FR-20) -------------------------------------------------


async def test_feedback_saved_once_and_truncated(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1)
    assert h.content.messages["ask_feedback"] in h.texts(U1)
    h.clear()

    await h.send(U1, "  Було   дуже круто!  " + "я" * 400)
    assert h.last_text(U1) == h.content.messages["feedback_thanks"]
    a = await attempt(session_factory, U1)
    assert a.feedback.startswith("Було дуже круто! яяя") and len(a.feedback) == 300

    h.clear()
    await h.send(U1, "ще одне речення")  # вдруге не приймаємо й не відповідаємо
    assert h.sent(U1) == []
    assert (await attempt(session_factory, U1)).feedback.startswith("Було дуже круто!")


async def test_feedback_skip_button(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1)
    await h.press(U1, "fb:skip")
    assert (await attempt(session_factory, U1)).feedback == repo.FEEDBACK_SKIPPED
    h.clear()
    await h.send(U1, "а тепер напишу")  # після пропуску текст ігнорується
    assert h.sent(U1) == []
    assert (await attempt(session_factory, U1)).feedback == repo.FEEDBACK_SKIPPED


async def test_feedback_not_requested_from_unfinished_players(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1, answers=1)
    h.clear()
    await h.send(U1, "це відповідь словами")
    assert h.last_text(U1) == h.content.messages["use_buttons"]
    assert (await attempt(session_factory, U1)).feedback is None


async def test_feedback_command_is_not_taken_as_feedback(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1)
    await h.send(U1, "/whatever")
    assert (await attempt(session_factory, U1)).feedback is None


async def test_whole_game_total_matches_synthetic_content(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1)
    assert (await attempt(session_factory, U1)).score == TOTAL
