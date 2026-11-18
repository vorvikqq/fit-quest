"""Наскрізно: апдейти Telegram → хендлери → GameService → TelegramMessenger (на підміненій сесії)."""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.db import repo
from bot.db.models import AttemptStatus
from bot.keyboards import START_GAME_DATA, AnsCb, GoCb
from tests.factory import TOTAL, make_content, register_users, set_running
from tests.harness import Harness

U = 7001


@pytest.fixture
async def h(session_factory):
    await register_users(session_factory, U)
    await set_running(session_factory)
    return Harness(session_factory, content=make_content())


async def attempt(session_factory, uid=U):
    async with session_factory() as s:
        return await repo.get_attempt_for_user(s, uid)


def kb_of(msg) -> list[str]:
    return [b.callback_data for row in msg.reply_markup.inline_keyboard for b in row]


async def press_correct(h, session_factory, uid=U):
    a = await attempt(session_factory, uid)
    entry = a.plan[a.cursor]
    q = h.game.index.info(entry["qid"]).question
    btn = entry["option_order"].index(q.correct)
    await h.press(uid, AnsCb(cursor=a.cursor, btn=btn).pack())


async def advance_to_cursor(h, session_factory, target: int, uid=U):
    """Тисне «Почати раунд» і правильні відповіді, поки спроба не дійде до `target`."""
    await h.press(uid, START_GAME_DATA)
    for _ in range(20):
        a = await attempt(session_factory, uid)
        if a.status == AttemptStatus.ROUND_INTRO:
            await h.press(uid, GoCb(cursor=a.cursor).pack())
            a = await attempt(session_factory, uid)
        if a.cursor >= target:
            return
        await press_correct(h, session_factory, uid)


async def test_start_button_sends_rules_and_round_card_with_go_button(h, session_factory):
    await h.press(U, START_GAME_DATA)
    assert h.texts(U)[0] == h.content.messages["rules"]
    card = h.sent(U)[-1]
    assert "Раунд Один" in card.text and kb_of(card) == [GoCb(cursor=0).pack()]


async def test_go_sends_question_with_letter_buttons_and_text_options(h, session_factory):
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    q = h.sent(U)[-1]
    assert q.text.startswith("Раунд 1/2 · Питання 1/2 · ⏱ 10 с")
    assert kb_of(q) == [AnsCb(cursor=0, btn=i).pack() for i in range(4)]
    assert [b.text for b in q.reply_markup.inline_keyboard[0]] == ["A", "B", "C", "D"]  # лише літери
    assert "<b>A)</b>" in q.text  # варіанти — у тексті


async def test_answer_edits_question_and_sends_next(h, session_factory):
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    h.clear()
    await press_correct(h, session_factory)

    (edit,) = h.edits()
    assert edit.text.endswith(h.content.messages["answer_accepted"]) and edit.reply_markup is None
    assert h.sent(U)[-1].text.startswith("Раунд 1/2 · Питання 2/2")
    assert h.answers()[-1].text is None  # без сповіщення: просто знімаємо спінер


async def test_repeated_press_gets_already_counted_toast(h, session_factory):
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    data = AnsCb(cursor=0, btn=0).pack()
    await h.press(U, data)
    h.clear()
    await h.press(U, data)  # дубль / старе повідомлення
    assert h.answers()[-1].text == h.content.messages["answer_stale"]
    assert h.sent(U) == []  # нічого нового не надіслано
    assert (await attempt(session_factory)).cursor == 1


async def test_text_during_game_gets_use_buttons_hint(h, session_factory):
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    await h.send(U, "я напишу відповідь словами")
    assert h.last_text(U) == h.content.messages["use_buttons"]
    h.clear()
    await h.send(U, "/whatever")  # команди не чіпаємо
    assert h.sent(U) == []


async def test_start_command_returns_to_current_question(h, session_factory):
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    h.clear()
    await h.send(U, "/start")
    resent = h.sent(U)[-1]
    assert resent.text.startswith("Раунд 1/2 · Питання 1/2")
    assert kb_of(resent)[0] == AnsCb(cursor=0, btn=0).pack()


async def test_full_game_through_handlers_ends_with_result(h, session_factory):
    await advance_to_cursor(h, session_factory, TOTAL)
    a = await attempt(session_factory)
    assert a.status == AttemptStatus.FINISHED and a.score == TOTAL
    assert f"<b>{TOTAL} з {TOTAL}</b>" in h.last_text(U)
    h.clear()
    await h.send(U, "дякую")  # після завершення вже не «обери кнопкою»
    assert h.sent(U) == []


async def test_photo_question_uses_caption_and_caches_file_id(h, session_factory):
    """q3 — бінарне питання з картинкою: підпис + кнопки в одному повідомленні."""
    await advance_to_cursor(h, session_factory, 2)
    (photo,) = h.photos(U)
    assert photo.caption.startswith("Раунд 2/2 · Питання 1/3")
    assert [b.text for b in photo.reply_markup.inline_keyboard[0]] == ["Так", "Ні"]
    assert photo.photo.__class__.__name__ == "FSInputFile"  # перший раз — завантаження файлу

    await press_correct(h, session_factory)  # відповідь редагує підпис, а не текст
    assert h.caption_edits()[0].caption.endswith(h.content.messages["answer_accepted"])

    # інший учасник отримує те саме фото вже за file_id з кешу
    await register_users(session_factory, U + 1)
    await advance_to_cursor(h, session_factory, 2, uid=U + 1)
    second = h.photos(U + 1)[0]
    assert isinstance(second.photo, str) and second.photo.startswith("FID-")


async def test_long_caption_is_sent_as_photo_then_question(h, session_factory):
    """DESIGN §6.3: підпис > 1024 → фото без підпису, наступним повідомленням питання з кнопками."""
    await advance_to_cursor(h, session_factory, 4)
    photo = h.photos(U)[-1]
    assert photo.caption is None and photo.reply_markup is None  # фото саме по собі
    question = h.sent(U)[-1]
    assert question.text.startswith("Раунд 2/2 · Питання 3/3") and kb_of(question)
    assert h.photo_then_question(U)


async def test_blocked_bot_is_flagged_and_game_continues(h, session_factory):
    original = h.session.make_request

    async def forbidden(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == U and "Питання" in (method.text or ""):
            raise TelegramForbiddenError(method, "bot was blocked by the user")
        return await original(bot, method, timeout)

    h.session.make_request = forbidden
    await h.press(U, START_GAME_DATA)
    await h.press(U, GoCb(cursor=0).pack())
    a = await attempt(session_factory)
    assert a.status == AttemptStatus.QUESTION and a.q_deadline is not None  # таймер усе одно йде
    assert a.q_message_id is None
    async with session_factory() as s:
        assert (await repo.get_user(s, U)).blocked_bot is True
