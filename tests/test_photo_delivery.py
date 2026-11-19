"""Швидка доставка картинок: одне завантаження на файл, автопрогрів, паралельна правка."""

from __future__ import annotations

import asyncio
import random


import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteMessage, SendMessage, SendPhoto
from aiogram.types import FSInputFile

from bot.db import repo
from bot.db.models import Phase
from bot.services.game import GameService
from bot.services.render import QuestionView
from tests.factory import Clock, FakeMessenger, make_content, register_users, set_running
from tests.harness import ADMIN_ID, Harness
from tests.test_admin_lifecycle import open_and_start

PHOTO = "images/r5_photo_01.png"
VIEW = QuestionView(text="Питання", image=PHOTO, split=False, buttons=(("Так", 0), ("Ні", 1)))


@pytest.fixture
async def h(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    return Harness(session_factory, content=make_content())


def slow_uploads(h: Harness, delay: float = 0.05):
    """Завантаження файлу (FSInputFile) триває, відправка за file_id миттєва."""
    original = h.session.make_request

    async def make_request(bot, method, timeout=None):
        if isinstance(method, SendPhoto) and isinstance(method.photo, FSInputFile):
            await asyncio.sleep(delay)
        return await original(bot, method, timeout)

    h.session.make_request = make_request


# --- одне завантаження на файл --------------------------------------------


async def test_concurrent_first_sends_upload_the_file_only_once(h):
    """200 учасників одночасно дійшли до фото при порожньому кеші: вантажимо файл лише раз."""
    slow_uploads(h)
    ids = list(range(500, 540))
    await asyncio.gather(*(h.game.messenger.send_question(uid, VIEW, 0) for uid in ids))

    photos = h.photos()
    uploads = [p for p in photos if isinstance(p.photo, FSInputFile)]
    by_id = [p for p in photos if isinstance(p.photo, str)]
    assert len(photos) == len(ids)  # кожен отримав фото
    assert len(uploads) == 1 and len(by_id) == len(ids) - 1
    assert {p.photo for p in by_id} == {by_id[0].photo}  # усі за одним file_id


async def test_each_image_has_its_own_lock(h):
    """Різні картинки завантажуються незалежно, а не по черзі."""
    slow_uploads(h, 0.5)
    other = QuestionView(text="x", image="images/r5_prompt_01.png", split=False, buttons=(("A", 0),))
    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        h.game.messenger.send_question(1, VIEW, 0), h.game.messenger.send_question(2, other, 0)
    )
    # паралельно ≈ 0,5 с; по черзі було б ≥ 1,0 с
    assert asyncio.get_running_loop().time() - started < 0.9


async def test_failed_upload_does_not_poison_the_lock(h):
    """Якщо завантаження зовсім не вдалось (мережа), наступний учасник спробує сам."""

    async def no_sleep(seconds: float) -> None:
        pass

    h.game.messenger._sleep = no_sleep  # без реальних пауз між повторами
    original = h.session.make_request
    calls = {"n": 0}

    async def flaky(bot, method, timeout=None):
        if isinstance(method, SendPhoto) and isinstance(method.photo, FSInputFile):
            calls["n"] += 1
            if calls["n"] <= 4:  # перша спроба й усі 3 повтори першого учасника
                raise TelegramNetworkError(method, "timeout")
        return await original(bot, method, timeout)

    h.session.make_request = flaky
    assert await h.game.messenger.send_question(1, VIEW, 0) is None  # перший не доставлено
    assert await h.game.messenger.send_question(2, VIEW, 0) is not None  # другий вантажить сам
    assert await h.game.messenger.send_question(3, VIEW, 0) is not None
    assert isinstance(h.photos()[-1].photo, str)  # третій уже за file_id


# --- прогрів ---------------------------------------------------------------


async def test_cache_status_counts_warm_images(h):
    assert await h.media.cache_status(h.content) == (0, 2)
    await h.game.messenger.send_question(1, VIEW, 0)  # завантажило одну картинку
    assert await h.media.cache_status(h.content) == (1, 2)


async def test_admin_panel_shows_image_cache_status(h):
    await h.send(ADMIN_ID, "/admin")
    assert "Картинки в кеші Telegram: 0 з 2 ⚠️" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/warmup")
    await h.send(ADMIN_ID, "/admin")
    assert "Картинки в кеші Telegram: 2 з 2 ✅" in h.last_text(ADMIN_ID)


async def test_warmup_cleanup_removes_helper_messages(h):
    result = await h.media.warmup(h.bot, ADMIN_ID, h.content, cleanup=True)
    assert len(result.uploaded) == 2
    deletes = [c for c in h.session.calls if isinstance(c, DeleteMessage)]
    assert len(deletes) == 2 and all(d.chat_id == ADMIN_ID for d in deletes)

    h.clear()
    again = await h.media.warmup(h.bot, ADMIN_ID, h.content, cleanup=True)
    assert again.uploaded == [] and len(again.cached) == 2 and h.photos() == []


async def test_manual_warmup_keeps_messages(h):
    await h.send(ADMIN_ID, "/warmup")
    assert not [c for c in h.session.calls if isinstance(c, DeleteMessage)]


async def test_start_quest_warms_images_before_announcing(h, session_factory):
    await open_and_start(h, session_factory, users=(6001,))
    # перед розсилкою «Квест стартував» картинки вже в кеші, а чат адміна не засмічено
    assert await h.media.cache_status(h.content) == (2, 2)
    assert len(h.photos(ADMIN_ID)) == 2
    assert len([c for c in h.session.calls if isinstance(c, DeleteMessage)]) == 2
    calls = [type(c).__name__ for c in h.session.calls]
    last_photo = max(i for i, n in enumerate(calls) if n == "SendPhoto")
    first_broadcast = next(
        i for i, c in enumerate(h.session.calls)
        if getattr(c, "chat_id", None) == 6001 and type(c).__name__ == "SendMessage"
        and c.text == h.content.messages["quest_started"]
    )  # fmt: skip
    assert last_photo < first_broadcast


async def test_warmup_failure_does_not_block_start(h, session_factory):
    original = h.session.make_request

    async def broken(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            raise TelegramNetworkError(method, "Telegram недоступний")
        return await original(bot, method, timeout)

    h.session.make_request = broken
    await open_and_start(h, session_factory, users=(6001,))
    async with session_factory() as s:
        assert (await repo.get_game_state(s)).phase == Phase.RUNNING
    assert h.content.messages["quest_started"] in h.texts(6001)


# --- правка попереднього питання не гальмує наступне -----------------------


class SlowCloseMessenger(FakeMessenger):
    """close_question «висить» (Telegram повільно відповідає на правку), доки його не відпустять."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def close_question(self, tg_id, message_id, view, suffix):
        await self.release.wait()
        await super().close_question(tg_id, message_id, view, suffix)


async def test_next_question_does_not_wait_for_slow_edit(session_factory):
    await register_users(session_factory, 101)
    await set_running(session_factory)
    messenger = SlowCloseMessenger()
    game = GameService(
        session_factory, make_content(), messenger, clock=Clock(), rng=random.Random(1)
    )
    await game.start_or_resume(101)
    await game.go(101, 0)
    async with session_factory() as s:
        attempt = await repo.get_attempt_for_user(s, 101)

    answer = asyncio.create_task(game.answer(101, 0, 0))
    for _ in range(100):  # чекаємо, поки з'явиться наступне питання, правка ще висить
        if len(messenger.questions(101)) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(messenger.questions(101)) == 2, "наступне питання має прийти, не чекаючи правки"
    assert not answer.done() and not messenger.of("close", 101)

    messenger.release.set()
    assert await answer is True
    assert len(messenger.of("close", 101)) == 1
    game.scheduler.cancel_all()
    assert attempt.cursor == 0


# --- мережеві збої Telegram (таймаут запиту) ------------------------------

TEXT_VIEW = QuestionView(text="Питання", image=None, split=False, buttons=(("A", 0), ("B", 1)))


def fail_sends(h: Harness, times: int, kind=SendMessage):
    """Перші `times` викликів `kind` падають мережевою помилкою (як `Request timeout error`)."""
    original = h.session.make_request
    state = {"left": times}

    async def make_request(bot, method, timeout=None):
        if isinstance(method, kind) and state["left"] > 0:
            state["left"] -= 1
            raise TelegramNetworkError(method, "Request timeout error")
        return await original(bot, method, timeout)

    h.session.make_request = make_request


async def test_network_error_is_retried_not_lost(h):
    """Раніше таймаут тихо губив питання: учасник лишався без повідомлення."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    h.game.messenger._sleep = fake_sleep
    fail_sends(h, 2)
    message_id = await h.game.messenger.send_question(1, TEXT_VIEW, 0)
    assert message_id is not None
    assert sleeps == [1.0, 2.0]  # пауза між повторами наростає


async def test_network_error_gives_up_after_retries(h):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    h.game.messenger._sleep = fake_sleep
    fail_sends(h, 100)
    assert await h.game.messenger.send_question(1, TEXT_VIEW, 0) is None
    assert len(sleeps) == 4  # 1 спроба + 3 повтори, і далі гра йде за серверним таймером


async def test_warmup_continues_after_one_failed_image(h):
    fail_sends(h, 1, SendPhoto)
    result = await h.media.warmup(h.bot, ADMIN_ID, h.content)
    assert len(result.failed) == 1 and len(result.uploaded) == 1


async def test_manual_warmup_reports_failed_images(h):
    fail_sends(h, 100, SendPhoto)
    await h.send(ADMIN_ID, "/warmup")
    assert "Не вдалося завантажити в Telegram (2)" in h.last_text(ADMIN_ID)


async def test_start_is_not_held_by_a_hanging_warmup(h, session_factory, monkeypatch):
    """Один завислий запит більше не затримує старт на хвилину (було 60 983 мс)."""
    from bot.handlers import admin as admin_handlers

    monkeypatch.setattr(admin_handlers, "WARMUP_TIMEOUT", 0.1)
    original = h.session.make_request

    async def hanging(bot, method, timeout=None):
        if isinstance(method, SendPhoto) and method.chat_id == ADMIN_ID:
            await asyncio.sleep(30)
        return await original(bot, method, timeout)

    h.session.make_request = hanging
    started = asyncio.get_running_loop().time()
    await open_and_start(h, session_factory, users=(6001,))
    assert asyncio.get_running_loop().time() - started < 5
    async with session_factory() as s:
        assert (await repo.get_game_state(s)).phase == Phase.RUNNING
    assert h.content.messages["quest_started"] in h.texts(6001)


async def test_manual_warmup_shows_the_real_reason_of_failure(h, caplog):
    """Раніше причина губилась, і адмін бачив лише «Не вдалося»: тепер вона в чаті й у логу."""
    fail_sends(h, 100, SendPhoto)
    with caplog.at_level("WARNING"):
        await h.send(ADMIN_ID, "/warmup")
    text = h.last_text(ADMIN_ID)
    assert "Причина: TelegramNetworkError" in text and "Request timeout error" in text
    assert any("не завантажено" in r.message and "Request timeout error" in r.message for r in caplog.records)


async def test_failure_reason_is_html_escaped(h):
    from aiogram.exceptions import TelegramBadRequest

    original = h.session.make_request

    async def rejecting(bot, method, timeout=None):
        if isinstance(method, SendPhoto):
            raise TelegramBadRequest(method, "<b>Bad Request</b> & more")
        return await original(bot, method, timeout)

    h.session.make_request = rejecting
    await h.send(ADMIN_ID, "/warmup")  # якби не екранувалось, Telegram відхилив би саме це повідомлення
    assert "&lt;b&gt;Bad Request&lt;/b&gt; &amp; more" in h.last_text(ADMIN_ID)
