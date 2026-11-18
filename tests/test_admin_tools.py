from __future__ import annotations

import csv
import io
import shutil

import pytest

from bot.config import PROJECT_ROOT
from bot.content.loader import file_sha256
from bot.db import repo
from bot.db.models import AttemptStatus, Phase
from bot.keyboards import START_GAME_DATA, Confirm
from tests.factory import make_content, register_users, set_running
from tests.harness import ADMIN_ID, Harness
from tests.test_admin_lifecycle import attempt, game, open_and_start, play, register

U1, U2, U3 = 6001, 6002, 6003
REAL_YAML = PROJECT_ROOT / "content" / "quest.yaml"


@pytest.fixture
async def h(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    return Harness(session_factory, content=make_content())


def yes(action, arg=0):
    return Confirm(action=action, arg=arg, yes=True).pack()


def no(action, arg=0):
    return Confirm(action=action, arg=arg, yes=False).pack()


def rows_of(doc) -> list[list[str]]:
    return list(csv.reader(io.StringIO(doc.document.data.decode("utf-8-sig"))))


async def played_event(h, session_factory):
    """U1 — усе правильно, U2 — усе хибно, U3 — зареєстрований без спроби."""
    await open_and_start(h, session_factory)
    await play(h, session_factory, U1, correct=True)
    await play(h, session_factory, U2, correct=False)
    h.clear()


# --- доступ ---------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    ["/stats", "/top", "/export", "/user 1", "/exclude 1", "/include 1", "/validate",
     "/reload_content", "/warmup", "/reset_me", "/reset_all"],
)  # fmt: skip
async def test_non_admin_cannot_use_admin_tools(h, session_factory, cmd):
    await register_users(session_factory, U1)
    await h.send(U1, cmd)
    assert h.sent(U1) == [] and h.documents(U1) == []


# --- /stats ---------------------------------------------------------------


async def test_stats_counts_everything_without_admins(h, session_factory):
    await played_event(h, session_factory)
    await register_users(session_factory, ADMIN_ID)
    async with session_factory.begin() as s:
        await repo.set_user_flag(s, ADMIN_ID, is_admin=True)
    await h.game.start_or_resume(ADMIN_ID)  # тестовий прохід адміна не має потрапити в цифри

    await h.send(ADMIN_ID, "/stats")
    text = h.last_text(ADMIN_ID)
    assert "Зареєстровано: <b>3</b>" in text
    assert "Грають: 0 · Завершили: 2" in text
    assert "Середній бал (завершили): 2.5 з 5" in text  # (5 + 0) / 2
    assert "Інше: 3" in text  # за статусом
    assert "Найскладніші питання" in text and "0%" in text  # хибні відповіді U2 тягнуть вниз


async def test_stats_on_empty_event(h, session_factory):
    await h.send(ADMIN_ID, "/stats")
    text = h.last_text(ADMIN_ID)
    assert "Зареєстровано: <b>0</b>" in text and "Середній бал" not in text


# --- /top -----------------------------------------------------------------


async def test_top_orders_by_score_and_links_profiles(h, session_factory):
    await played_event(h, session_factory)
    await h.send(ADMIN_ID, "/top")
    lines = h.last_text(ADMIN_ID).splitlines()
    assert lines[0].startswith("<b>Топ-10</b>")
    assert lines[1].startswith(f'1. <a href="tg://user?id={U1}">Учасник {U1}</a> @u{U1} — 5/5')
    assert lines[2].startswith(f'2. <a href="tg://user?id={U2}">Учасник {U2}</a>') and "— 0/5" in lines[2]
    assert len(lines) == 3  # U3 не грав


async def test_top_limit_and_validation_and_empty(h, session_factory):
    await h.send(ADMIN_ID, "/top")
    assert "ще ніхто не завершив" in h.last_text(ADMIN_ID)

    await played_event(h, session_factory)
    await h.send(ADMIN_ID, "/top 1")
    assert len(h.last_text(ADMIN_ID).splitlines()) == 2
    for bad in ("0", "101", "abc", "-1"):
        await h.send(ADMIN_ID, f"/top {bad}")
        assert "Використання" in h.last_text(ADMIN_ID)


async def test_top_escapes_names(h, session_factory):
    await played_event(h, session_factory)
    async with session_factory.begin() as s:
        await s.execute(__import__("sqlalchemy").text("UPDATE users SET full_name = '<b>Хакер</b> & Ко' WHERE tg_id = 6001"))
    await h.send(ADMIN_ID, "/top 1")
    assert "&lt;b&gt;Хакер&lt;/b&gt; &amp; Ко" in h.last_text(ADMIN_ID)


# --- /exclude, /include ----------------------------------------------------


async def test_exclude_removes_from_top_and_include_returns(h, session_factory):
    await played_event(h, session_factory)
    await h.send(ADMIN_ID, f"/exclude {U1}")
    assert "виключено" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/top")
    assert f"tg://user?id={U1}" not in h.last_text(ADMIN_ID)

    await h.send(ADMIN_ID, f"/include @u{U1}")
    assert "повернено" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/top")
    assert f"tg://user?id={U1}" in h.last_text(ADMIN_ID)


async def test_exclude_unknown_user_and_missing_argument(h, session_factory):
    await h.send(ADMIN_ID, "/exclude 999999")
    assert "не знайдено" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/exclude")
    assert "Використання" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/include @nobody")
    assert "не знайдено" in h.last_text(ADMIN_ID)


# --- /user ----------------------------------------------------------------


async def test_user_card_shows_registration_progress_and_answers(h, session_factory):
    await played_event(h, session_factory)
    await h.send(ADMIN_ID, f"/user {U1}")
    card = h.last_text(ADMIN_ID)
    assert f"Учасник {U1}" in card and f"id <code>{U1}</code>" in card
    assert "Інше" in card and "FINISHED" in card and "відповідей 5 з 5" in card and "бал 5" in card
    assert card.count("✅") == 5 and "q1 ✅" in card

    await h.send(ADMIN_ID, f"/user @u{U2}")
    assert h.last_text(ADMIN_ID).count("❌") == 5

    await h.send(ADMIN_ID, f"/user {U3}")
    assert "Спроби немає" in h.last_text(ADMIN_ID)


async def test_user_card_errors(h, session_factory):
    await h.send(ADMIN_ID, "/user")
    assert "Використання" in h.last_text(ADMIN_ID)
    await h.send(ADMIN_ID, "/user 42")
    assert "не знайдено" in h.last_text(ADMIN_ID)


# --- /export --------------------------------------------------------------


async def test_export_sends_three_csv_files(h, session_factory):
    await played_event(h, session_factory)
    await h.send(U1, "Було дуже круто")  # відгук
    await h.press(U2, "fb:skip")  # пропуск не потрапляє у feedback.csv
    h.clear()

    await h.send(ADMIN_ID, "/export")
    docs = {d.document.filename: d for d in h.documents(ADMIN_ID)}
    assert set(docs) == {"participants.csv", "answers.csv", "feedback.csv"}
    assert docs["participants.csv"].document.data.startswith(b"\xef\xbb\xbf")  # BOM для Excel

    participants = rows_of(docs["participants.csv"])
    header, body = participants[0], participants[1:]
    assert len(body) == 3  # U1, U2, U3 (усі зареєстровані)
    col = {name: i for i, name in enumerate(header)}
    by_id = {row[col["tg_id"]]: row for row in body}
    assert by_id[str(U1)][col["score"]] == "5" and by_id[str(U1)][col["place"]] == "1"
    assert by_id[str(U2)][col["place"]] == "2"
    assert by_id[str(U3)][col["attempt_status"]] == "" and by_id[str(U3)][col["place"]] == ""

    answers = rows_of(docs["answers.csv"])
    assert len(answers) - 1 == 10  # 2 гравці × 5 питань
    acol = {name: i for i, name in enumerate(answers[0])}
    assert {r[acol["qid"]] for r in answers[1:]} == {"q1", "q2", "q3", "q4", "q5"}

    feedback = rows_of(docs["feedback.csv"])
    assert [r[3] for r in feedback[1:]] == ["Було дуже круто"]


async def test_export_neutralizes_csv_injection(h, session_factory):
    await played_event(h, session_factory)
    async with session_factory.begin() as s:
        await s.execute(__import__("sqlalchemy").text("UPDATE users SET full_name = '=HYPERLINK(\"http://evil\")' WHERE tg_id = 6001"))
    await h.send(ADMIN_ID, "/export")
    participants = next(d for d in h.documents(ADMIN_ID) if d.document.filename == "participants.csv")
    body = participants.document.data.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and ",=HYPERLINK" not in body


async def test_export_on_empty_database_still_returns_headers(h, session_factory):
    await h.send(ADMIN_ID, "/export")
    docs = h.documents(ADMIN_ID)
    assert len(docs) == 3 and all(len(rows_of(d)) == 1 for d in docs)


# --- /validate ------------------------------------------------------------


async def test_validate_reports_todo_for_real_file(h, session_factory):
    await h.send(ADMIN_ID, "/validate")
    text = "\n".join(h.texts(ADMIN_ID))
    assert "питань: 55" in text and "Незаповнених (TODO): 16" in text and "r4q01" in text
    assert text.startswith("<pre>")


async def test_validate_reads_file_from_disk_and_reports_errors(session_factory, tmp_path):
    broken = tmp_path / "quest.yaml"
    shutil.copy(REAL_YAML, broken)
    broken.write_text(broken.read_text(encoding="utf-8").replace("correct: 1\n            explanation: \"Двійкова", "correct: 9\n            explanation: \"Двійкова", 1), encoding="utf-8")
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h2 = Harness(session_factory, content_path=broken)
    await h2.send(ADMIN_ID, "/validate")
    text = "\n".join(h2.texts(ADMIN_ID))
    assert "Помилок: 1" in text and "r1q01" in text and "поза межами" in text

    (tmp_path / "bad.yaml").write_text("rounds: [", encoding="utf-8")
    h3 = Harness(session_factory, content_path=tmp_path / "bad.yaml")
    await h3.send(ADMIN_ID, "/validate")
    assert "Файл не прочитано" in h3.last_text(ADMIN_ID)


# --- /reload_content ------------------------------------------------------


@pytest.fixture
def editable_yaml(tmp_path):
    path = tmp_path / "quest.yaml"
    shutil.copytree(PROJECT_ROOT / "content" / "images", tmp_path / "images")
    shutil.copy(REAL_YAML, path)
    return path


def edit(path, old, new):
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")


async def test_reload_applies_new_text_without_restart(session_factory, editable_yaml):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h2 = Harness(session_factory, content_path=editable_yaml)
    assert h2.game.content.messages["time_is_up"] == "⏰ Час вийшов"

    edit(editable_yaml, '"⏰ Час вийшов"', '"⏰ Час вичерпано"')
    await h2.send(ADMIN_ID, "/reload_content")
    assert "перечитано" in h2.last_text(ADMIN_ID)
    assert h2.game.content.messages["time_is_up"] == "⏰ Час вичерпано"
    assert h2.dp["content"].messages["time_is_up"] == "⏰ Час вичерпано"  # хендлери теж бачать нове


async def test_reload_refuses_broken_file_and_keeps_old_content(session_factory, editable_yaml):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h2 = Harness(session_factory, content_path=editable_yaml)
    edit(editable_yaml, "correct: 1\n            explanation: \"Двійкова", "correct: 9\n            explanation: \"Двійкова")
    await h2.send(ADMIN_ID, "/reload_content")
    assert "лишаю попередню версію" in h2.last_text(ADMIN_ID)
    assert h2.game.content.question_count == 55

    editable_yaml.write_text("rounds: [", encoding="utf-8")
    await h2.send(ADMIN_ID, "/reload_content")
    assert "Файл не прочитано" in h2.last_text(ADMIN_ID)


async def test_reload_forbidden_during_running_if_file_changed(session_factory, editable_yaml):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
        await repo.update_game_state(s, phase="RUNNING", content_hash=file_sha256(editable_yaml))
    h2 = Harness(session_factory, content_path=editable_yaml)

    await h2.send(ADMIN_ID, "/reload_content")  # файл не змінився з моменту старту: можна
    assert "Файл не змінився" in h2.last_text(ADMIN_ID)

    edit(editable_yaml, '"⏰ Час вийшов"', '"⏰ Інше"')
    await h2.send(ADMIN_ID, "/reload_content")
    assert "заборонено" in h2.last_text(ADMIN_ID)
    assert h2.game.content.messages["time_is_up"] == "⏰ Час вийшов"

    async with session_factory.begin() as s:  # після закриття квесту правки дозволені
        await repo.update_game_state(s, phase="CLOSED")
    await h2.send(ADMIN_ID, "/reload_content")
    assert "перечитано" in h2.last_text(ADMIN_ID)


async def test_reload_refuses_when_attempts_reference_removed_questions(session_factory, editable_yaml):
    await register_users(session_factory, U1)
    async with session_factory.begin() as s:
        await repo.create_attempt(s, U1, [{"qid": "no_such_question", "option_order": [0, 1]}], __import__("datetime").datetime.now(__import__("datetime").UTC))
    h2 = Harness(session_factory, content_path=editable_yaml)
    await h2.send(ADMIN_ID, "/reload_content")
    assert "no_such_question" in h2.last_text(ADMIN_ID)


# --- /warmup --------------------------------------------------------------


async def test_warmup_uploads_missing_images_once(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h2 = Harness(session_factory)  # справжній контент: 10 картинок
    await h2.send(ADMIN_ID, "/warmup")
    assert "завантажено 10, уже було 0" in h2.last_text(ADMIN_ID)
    assert len(h2.photos(ADMIN_ID)) == 10
    async with session_factory() as s:
        row = await repo.get_media(s, "images/r5_photo_01.png")
    assert row is not None and row.file_id.startswith("FID-") and len(row.sha256) == 64

    h2.clear()
    await h2.send(ADMIN_ID, "/warmup")
    assert "завантажено 0, уже було 10" in h2.last_text(ADMIN_ID)
    assert h2.photos(ADMIN_ID) == []


async def test_warmup_reuploads_changed_file_and_reports_missing(session_factory, tmp_path):
    (tmp_path / "images").mkdir()
    shutil.copy(PROJECT_ROOT / "content" / "images" / "r5_photo_01.png", tmp_path / "images" / "r5_photo_01.png")
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h2 = Harness(session_factory)
    h2.media.content_root = tmp_path  # у каталозі лише одна картинка з десяти
    await h2.send(ADMIN_ID, "/warmup")
    text = h2.last_text(ADMIN_ID)
    assert "завантажено 1" in text and "Немає файлів" in text and "r5_prompt_05.png" in text

    (tmp_path / "images" / "r5_photo_01.png").write_bytes(
        (tmp_path / "images" / "r5_photo_01.png").read_bytes() + b"0"
    )  # файл змінився → новий sha256
    h2.media._sha_cache.clear()
    h2.clear()
    await h2.send(ADMIN_ID, "/warmup")
    assert "завантажено 1" in h2.last_text(ADMIN_ID)


# --- /reset_me ------------------------------------------------------------


async def test_reset_me_deletes_own_attempt_and_timer(h, session_factory):
    await register_users(session_factory, ADMIN_ID)
    await set_running(session_factory)
    await h.game.start_or_resume(ADMIN_ID)
    a = await attempt(session_factory, ADMIN_ID)
    await h.game.go(ADMIN_ID, 0)
    assert a.id in h.game.scheduler

    await h.send(ADMIN_ID, "/reset_me")
    assert "видалено" in h.last_text(ADMIN_ID)
    assert await attempt(session_factory, ADMIN_ID) is None
    assert a.id not in h.game.scheduler

    await h.press(ADMIN_ID, START_GAME_DATA)  # можна пройти знову (FR-31)
    assert (await attempt(session_factory, ADMIN_ID)).status == AttemptStatus.ROUND_INTRO

    await h.send(ADMIN_ID, "/reset_me")
    await h.send(ADMIN_ID, "/reset_me")
    assert "немає спроби" in h.last_text(ADMIN_ID)


async def test_reset_me_does_not_touch_other_users(h, session_factory):
    await open_and_start(h, session_factory, users=(U1,))
    await play(h, session_factory, U1)
    await register_users(session_factory, ADMIN_ID)
    await h.send(ADMIN_ID, "/reset_me")
    assert (await attempt(session_factory, U1)).status == AttemptStatus.FINISHED


# --- /reset_all -----------------------------------------------------------


async def test_reset_all_needs_two_confirmations_and_wipes_everything(h, session_factory):
    await played_event(h, session_factory)
    async with session_factory.begin() as s:
        await repo.update_game_state(s, phase="RESULTS", closes_at=None)
    await h.send(ADMIN_ID, "/reset_all")
    assert "Скинути все?" in h.last_text(ADMIN_ID) and "Кодове слово лишиться" in h.last_text(ADMIN_ID)

    await h.press(ADMIN_ID, yes("reset_all"))  # перше «так» → друге підтвердження
    assert "Ще раз, остаточно" in h.edits()[-1].text
    assert (await attempt(session_factory, U1)) is not None  # ще нічого не стерто

    await h.press(ADMIN_ID, yes("reset_all", arg=1))
    st = await game(session_factory)
    assert (st.phase, st.registration_open, st.access_code) == (Phase.DRAFT, False, "Secret")
    assert st.started_at is None and st.content_hash is None
    async with session_factory() as s:
        counters = await repo.get_counters(s)
        assert (counters.registered, counters.finished) == (0, 0)
        assert await repo.get_user(s, U1) is None
        assert await repo.get_user(s, ADMIN_ID) is not None  # адмін лишається


async def test_reset_all_can_be_cancelled_at_either_step(h, session_factory):
    await played_event(h, session_factory)
    await h.send(ADMIN_ID, "/reset_all")
    await h.press(ADMIN_ID, no("reset_all"))
    await h.send(ADMIN_ID, "/reset_all")
    await h.press(ADMIN_ID, yes("reset_all"))
    await h.press(ADMIN_ID, no("reset_all", arg=1))
    assert (await attempt(session_factory, U1)) is not None


async def test_reset_all_refused_while_quest_running(h, session_factory):
    await played_event(h, session_factory)
    assert (await game(session_factory)).phase == Phase.RUNNING
    await h.send(ADMIN_ID, "/reset_all")
    assert "/close_quest" in h.last_text(ADMIN_ID)
    await h.press(ADMIN_ID, yes("reset_all", arg=1))  # навіть застаріла кнопка не діє
    assert (await attempt(session_factory, U1)) is not None


async def test_event_can_be_rehearsed_twice_after_reset(h, session_factory):
    """DESIGN §15.3: прогін → /reset_all → справжня подія з нуля."""
    await played_event(h, session_factory)
    await h.send(ADMIN_ID, "/close_quest")
    await h.press(ADMIN_ID, yes("close_quest"))
    await h.wait_background()
    await h.send(ADMIN_ID, "/reset_all")
    await h.press(ADMIN_ID, yes("reset_all"))
    await h.press(ADMIN_ID, yes("reset_all", arg=1))

    await h.send(ADMIN_ID, "/open_reg")
    await h.press(ADMIN_ID, yes("open_reg"))
    await register(h, U1)  # той самий акаунт реєструється знову
    assert (await game(session_factory)).phase == Phase.REGISTRATION
