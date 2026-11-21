"""/results: учасник сам запитує розбір (що відповів, що правильно й чому) після публікації."""

from __future__ import annotations

import pytest

from bot.db import repo
from bot.keyboards import Confirm
from bot.services.results import render_review
from tests.factory import make_content
from tests.harness import ADMIN_ID, Harness
from tests.test_admin_lifecycle import attempt, open_and_start, play

U1, U2, U3 = 6001, 6002, 6003


def yes(action):
    return Confirm(action=action, yes=True).pack()


@pytest.fixture
async def h(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    content = make_content()
    q1 = content.rounds[0].sections[0].questions[0]
    q1.explanation = "Бо 1+1 це два"
    content.rounds[1].sections[0].questions[1].explanation = "Бо так вирішили організатори"
    return Harness(session_factory, content=content)


async def finish_event(h, session_factory, publish=True):
    """U1: усе правильно; U2: усе хибно; U3 зареєстрований, але не грав."""
    await open_and_start(h, session_factory)
    await play(h, session_factory, U1, correct=True)
    await play(h, session_factory, U2, correct=False)
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    if publish:
        await h.send(ADMIN_ID, "/publish_results")
        await h.press(ADMIN_ID, yes("publish_results"))
        await h.wait_background()
    h.clear()


# --- доступ ---------------------------------------------------------------


async def test_not_available_until_results_are_published(h, session_factory):
    await finish_event(h, session_factory, publish=False)  # етап CLOSED
    await h.send(U1, "/results")
    assert h.texts(U1) == [h.content.messages["review_not_published"]]


async def test_not_available_during_the_quest(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1, correct=True)
    h.clear()
    await h.send(U1, "/results")
    assert h.texts(U1) == [h.content.messages["review_not_published"]]


async def test_user_without_attempt_gets_a_clear_message(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U3, "/results")  # зареєстрований, але не починав
    await h.send(99999, "/review")  # узагалі невідомий акаунт
    assert h.texts(U3) == [h.content.messages["review_no_attempt"]]
    assert h.texts(99999) == [h.content.messages["review_no_attempt"]]


async def test_review_alias_works(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U1, "/review")
    assert len(h.texts(U1)) >= 2


# --- вміст -----------------------------------------------------------------


async def test_winner_sees_place_and_no_correct_answer_lines(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U1, "/results")
    header, *review = h.texts(U1)
    assert "<b>1</b> з 2" in header and "<b>5 з 5</b>" in header
    body = "\n\n".join(review)
    assert body.count("✅") == 5 and "❌" not in body
    assert "Правильна відповідь" not in body  # правильні відповіді не дублюємо
    assert "💡 Бо 1+1 це два" in body  # пояснення лишається


async def test_wrong_answers_show_choice_correct_answer_and_explanation(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U2, "/results")
    header, *review = h.texts(U2)
    assert "<b>2</b> з 2" in header and "<b>0 з 5</b>" in header
    body = "\n\n".join(review)
    assert body.count("❌") == 5 and "✅" not in body
    assert body.count("Твоя відповідь:") == 5 and body.count("Правильна відповідь:") == 5
    assert "💡 Бо 1+1 це два" in body and "💡 Бо так вирішили організатори" in body


async def test_your_answer_text_matches_the_button_pressed_despite_shuffle(h, session_factory):
    """У БД лежить індекс в оригінальному порядку: розбір має показати саме той текст, що обрав учасник."""
    await finish_event(h, session_factory)
    a = await attempt(session_factory, U2)
    async with session_factory() as s:
        answers = await repo.answers_by_attempt(s, [a.id])
    review = "\n\n".join(render_review(h.game.index, a, answers[a.id]))
    for qid, ans in answers[a.id].items():
        info = h.game.index.info(qid)
        options = info.section.labels if info.section.question_type == "binary" else info.question.options
        assert f"Твоя відповідь: {options[ans.chosen]}" in review, qid


async def test_timeout_and_unfinished_are_marked(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1, answers=1, correct=True)  # одна відповідь, далі квест закрили
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    await h.send(ADMIN_ID, "/publish_results")
    await h.press(ADMIN_ID, yes("publish_results"))
    await h.wait_background()
    h.clear()
    await h.send(U1, "/results")
    body = "\n\n".join(h.texts(U1)[1:])
    assert body.count("✅") == 1 and body.count("➖") == 4  # не дійшов до решти
    assert "Твоя відповідь" not in body.split("➖")[1]  # для непройдених питань вибору немає


async def test_timed_out_question_says_you_did_not_answer(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await h.press(U1, "game:start")
    a = await attempt(session_factory, U1)
    from bot.keyboards import GoCb

    await h.press(U1, GoCb(cursor=a.cursor).pack())
    await h.game.on_timeout(a.id, 0)  # перше питання прострочено
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    await h.send(ADMIN_ID, "/publish_results")
    await h.press(ADMIN_ID, yes("publish_results"))
    await h.wait_background()
    h.clear()
    await h.send(U1, "/results")
    body = "\n\n".join(h.texts(U1)[1:])
    assert "⏰" in body and h.content.messages["review_timeout"] in body
    assert "Правильна відповідь: b" in body  # правильну все одно показуємо


async def test_excluded_user_gets_review_without_place(h, session_factory):
    await finish_event(h, session_factory)
    async with session_factory.begin() as s:
        await repo.set_user_flag(s, U2, excluded=True)
    await h.send(U2, "/results")
    header = h.texts(U2)[0]
    assert "Твій результат" in header and "Твоє місце" not in header and "<b>0 з 5</b>" in header


async def test_place_counts_only_ranked_participants(h, session_factory):
    await finish_event(h, session_factory)
    async with session_factory.begin() as s:
        await repo.set_user_flag(s, U1, excluded=True)  # переможця виключили після публікації
    await h.send(U2, "/results")
    assert "<b>1</b> з 1" in h.texts(U2)[0]


# --- захист від спаму й приватність --------------------------------------------


async def test_repeated_requests_are_rate_limited(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U1, "/results")
    await h.send(U1, "/results")
    sent_before = len(h.texts(U1))
    await h.send(U1, "/results")  # третій за хвилину
    assert h.texts(U1)[sent_before:] == [h.content.messages["review_slow_down"]]


async def test_users_see_only_their_own_review(h, session_factory):
    await finish_event(h, session_factory)
    await h.send(U1, "/results")
    await h.send(U2, "/results")
    assert "✅" in "".join(h.texts(U1)) and "❌" not in "".join(h.texts(U1))
    assert "❌" in "".join(h.texts(U2)) and "✅" not in "".join(h.texts(U2))


async def test_results_command_is_ignored_in_groups(h, session_factory):
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, Update, User

    await finish_event(h, session_factory)
    update = Update(
        update_id=999,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=-100, type="group"),
            from_user=User(id=U1, is_bot=False, first_name="x"),
            text="/results",
        ),
    )
    await h.dp.feed_update(h.bot, update)
    assert h.sent(-100) == []
