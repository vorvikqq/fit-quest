from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from bot.config import PROJECT_ROOT
from bot.content.loader import (
    CAPTION_LIMIT,
    RESERVE,
    MESSAGE_LIMIT,
    REQUIRED_MESSAGES,
    ContentError,
    load_and_validate,
    load_content,
    parse_content,
    validate_content,
)

VALID: dict = {
    "meta": {
        "title": "Квест",
        "promo": {"code": "CODE", "text": "промо"},
        "defaults": {"time_limit": 20, "shuffle_options": True, "shuffle_questions": False},
    },
    "messages": {key: f"текст {key}" for key in REQUIRED_MESSAGES},
    "rounds": [
        {
            "id": "r1",
            "title": "Раунд 1",
            "sections": [
                {
                    "id": "r1s1",
                    "question_type": "choice",
                    "questions": [
                        {
                            "id": "r1q1",
                            "text": "2+2?",
                            "options": ["3", "4", "5", "6"],
                            "correct": 1,
                            "explanation": "4",
                        }
                    ],
                },
                {
                    "id": "r1s2",
                    "question_type": "binary",
                    "labels": ["Так", "Ні"],
                    "questions": [
                        {"id": "r1q2", "text": "Земля кругла", "correct": 0, "explanation": "так"}
                    ],
                },
            ],
        }
    ],
}


def make(mutate=None) -> dict:
    data = copy.deepcopy(VALID)
    if mutate:
        mutate(data)
    return data


def q_choice(data: dict) -> dict:
    return data["rounds"][0]["sections"][0]["questions"][0]


def q_binary(data: dict) -> dict:
    return data["rounds"][0]["sections"][1]["questions"][0]


def check(data: dict, root: Path = Path(".")):
    return validate_content(parse_content(data), root)


def messages_of(issues) -> list[str]:
    return [i.message for i in issues]


# --- базовий випадок -----------------------------------------------------


def test_valid_content_has_no_issues():
    report = check(make())
    assert report.issues == []
    assert report.ok and not report.has_todo


# --- унікальність id -----------------------------------------------------


def test_duplicate_question_id_across_rounds():
    def mutate(d):
        second = copy.deepcopy(d["rounds"][0])
        second["id"] = "r2"
        second["sections"][0]["id"] = "r2s1"
        second["sections"][1]["id"] = "r2s2"
        d["rounds"].append(second)  # ті ж id питань r1q1/r1q2

    report = check(make(mutate))
    assert {i.where for i in report.errors} == {"r1q1", "r1q2"}


def test_duplicate_round_and_section_id():
    def mutate(d):
        d["rounds"].append(copy.deepcopy(d["rounds"][0]))
        for s in d["rounds"][1]["sections"]:
            for q in s["questions"]:
                q["id"] += "x"

    report = check(make(mutate))
    assert {"r1", "r1s1", "r1s2"} <= {i.where for i in report.errors}


# --- choice / binary -----------------------------------------------------


@pytest.mark.parametrize("options", [["один"], ["a", "b", "c", "d", "e"], None])
def test_choice_needs_2_to_4_options(options):
    def mutate(d):
        q_choice(d)["options"] = options
        q_choice(d)["correct"] = 0

    report = check(make(mutate))
    assert any(i.where == "r1q1" and "потрібно 2–4" in i.message for i in report.errors)


def test_choice_correct_out_of_range():
    report = check(make(lambda d: q_choice(d).update(correct=4)))
    assert any("поза межами" in m for m in messages_of(report.errors))


def test_negative_correct_is_error():
    report = check(make(lambda d: q_choice(d).update(correct=-1)))
    assert any("поза межами" in m for m in messages_of(report.errors))


def test_binary_correct_out_of_range():
    report = check(make(lambda d: q_binary(d).update(correct=2)))
    assert any(i.where == "r1q2" and "поза межами" in i.message for i in report.errors)


@pytest.mark.parametrize("labels", [None, ["одна"], ["a", "b", "c"]])
def test_binary_requires_exactly_two_labels(labels):
    def mutate(d):
        d["rounds"][0]["sections"][1]["labels"] = labels

    report = check(make(mutate))
    assert any(i.where == "r1s2" and "2 `labels`" in i.message for i in report.errors)


def test_binary_must_not_have_options():
    report = check(make(lambda d: q_binary(d).update(options=["a", "b"])))
    assert any(i.where == "r1q2" for i in report.errors)


def test_choice_must_not_have_labels():
    report = check(make(lambda d: d["rounds"][0]["sections"][0].update(labels=["a", "b"])))
    assert any(i.where == "r1s1" for i in report.errors)


# --- картинки ------------------------------------------------------------


def test_missing_image_is_error(tmp_path: Path):
    report = check(make(lambda d: q_binary(d).update(image="images/none.png")), tmp_path)
    assert any("не знайдено" in m for m in messages_of(report.errors))


def test_existing_image_is_ok(tmp_path: Path):
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "a.png").write_bytes(b"x")
    report = check(make(lambda d: q_binary(d).update(image="images/a.png")), tmp_path)
    assert report.issues == []


# --- ліміти довжини ------------------------------------------------------


def _with_image(tmp_path: Path, d: dict) -> None:
    (tmp_path / "a.png").write_bytes(b"x")
    q_choice(d)["image"] = "a.png"


def test_caption_limit_boundary_with_image(tmp_path: Path):
    # "A) " (3) + варіант; решта варіантів по 1 символу + 3 → рахуємо точно
    def build(total: int):
        def mutate(d):
            _with_image(tmp_path, d)
            q = q_choice(d)
            q["options"] = ["x", "y"]
            q["correct"] = 0
            # рендер: text + "\nA) x\nB) y" = len(text) + 10
            q["text"] = "ы" * (total - 10 - RESERVE)

        return make(mutate)

    at_limit = check(build(CAPTION_LIMIT), tmp_path)
    assert at_limit.issues == []

    over = check(build(CAPTION_LIMIT + 1), tmp_path)
    assert over.ok  # не помилка: бот розділить повідомлення (DESIGN §6.3)
    assert len(over.warnings) == 1 and over.warnings[0].where == "r1q1"


def test_no_warning_over_1024_without_image():
    def mutate(d):
        q_binary(d)["text"] = "а" * (CAPTION_LIMIT + 500)

    assert check(make(mutate)).issues == []


def test_message_limit_is_error():
    report = check(make(lambda d: q_binary(d).update(text="а" * (MESSAGE_LIMIT + 1))))
    assert any("ліміт повідомлення" in m for m in messages_of(report.errors))


def test_html_in_question_text_must_be_valid_for_telegram():
    for bad in ("2 < 3", "<script>x</script>", "<b>не закрито", "a</i>"):
        report = check(make(lambda d, b=bad: q_binary(d).update(text=b)))
        assert any("HTML у тексті" in m for m in messages_of(report.errors)), bad
    ok = check(make(lambda d: q_binary(d).update(text="<b>жирний</b> і <a href='x'>лінк</a> &lt; ok")))
    assert ok.issues == []


def test_choice_options_are_counted_verbatim_not_as_html():
    def mutate(d):
        q_choice(d)["options"] = ["<<", "#include <cmath>", ">>"]
        q_choice(d)["correct"] = 0

    assert check(make(mutate)).issues == []  # варіанти екрануються ботом, тож `<` у них не помилка


def test_html_tags_do_not_count():
    # 1000 символів тексту, обгорнутих у довгі теги, не мають перевищити ліміт
    text = "<b>" + "а" * 1000 + "</b>" + "<a href='https://example.com/" + "z" * 200 + "'></a>"

    def mutate(d):
        q_binary(d)["text"] = text

    assert check(make(mutate)).issues == []


def test_emoji_count_as_two_units():
    def mutate(d):
        q_binary(d)["text"] = "😀" * (MESSAGE_LIMIT // 2 + 1)  # 4098 UTF-16 units

    assert any("ліміт повідомлення" in m for m in messages_of(check(make(mutate)).errors))


# --- TODO ----------------------------------------------------------------


def test_todo_field_is_reported():
    report = check(make(lambda d: q_choice(d).update(todo="перевірити")))
    assert report.has_todo and report.ok
    assert messages_of(report.todos) == ["перевірити"]


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_todo_is_ignored(blank):
    assert check(make(lambda d: q_choice(d).update(todo=blank))).issues == []


def test_correct_null_is_todo_not_error():
    report = check(make(lambda d: q_choice(d).update(correct=None)))
    assert report.ok and report.has_todo
    assert len(report.todos) == 1 and "не вказано `correct`" in report.todos[0].message


def test_several_todo_reasons_give_one_entry_per_question():
    def mutate(d):
        q_binary(d).update(text="TODO: текст", correct=None, explanation="TODO", todo="чекаємо")

    report = check(make(mutate))
    assert [i.where for i in report.todos] == ["r1q2"]
    assert report.todos[0].message.startswith("чекаємо; ")


@pytest.mark.parametrize("field", ["text", "explanation"])
@pytest.mark.parametrize("value", ["TODO: дописати", "  todo", "<b>TODO: назва</b>"])
def test_text_starting_with_todo(field, value):
    report = check(make(lambda d: q_binary(d).update({field: value})))
    assert report.has_todo and report.ok


def test_option_starting_with_todo():
    def mutate(d):
        q_choice(d)["options"] = ["TODO", "b", "c"]

    assert check(make(mutate)).has_todo


def test_todo_word_inside_text_is_not_todo():
    report = check(make(lambda d: q_binary(d).update(text="Список TODO у Jira")))
    assert not report.has_todo


def test_todo_in_message_and_intro():
    def mutate(d):
        d["messages"]["welcome"] = "TODO привітання"
        d["rounds"][0]["intro"] = "TODO вступ"

    report = check(make(mutate))
    assert {"messages.welcome", "r1"} <= {i.where for i in report.todos}


# --- meta / messages / налаштування -------------------------------------


def test_missing_required_message():
    def mutate(d):
        del d["messages"]["welcome"]

    report = check(make(mutate))
    assert [i.where for i in report.errors] == ["messages.welcome"]


def test_non_positive_time_limit():
    report = check(make(lambda d: d["rounds"][0]["sections"][0].update(time_limit=0)))
    assert any(i.where == "r1s1" for i in report.errors)


def test_empty_round_section_and_questions():
    def mutate(d):
        d["rounds"].append({"id": "r2", "title": "порожній", "sections": []})
        d["rounds"][0]["sections"][0]["questions"] = []

    report = check(make(mutate))
    assert {"r2", "r1s1"} <= {i.where for i in report.errors}


def test_all_problems_are_collected_at_once():
    def mutate(d):
        q_choice(d)["correct"] = 9
        q_binary(d)["image"] = "nope.png"
        q_binary(d)["todo"] = "later"

    report = check(make(mutate))
    assert len(report.errors) == 2 and len(report.todos) == 1


def test_settings_inheritance():
    def mutate(d):
        d["rounds"][0]["time_limit"] = 15
        d["rounds"][0]["shuffle_questions"] = True
        d["rounds"][0]["sections"][1]["time_limit"] = 40

    content = parse_content(make(mutate))
    rnd = content.rounds[0]
    first = content.settings_for(rnd, rnd.sections[0])
    second = content.settings_for(rnd, rnd.sections[1])
    assert (first.time_limit, first.shuffle_questions, first.shuffle_options) == (15, True, True)
    assert (second.time_limit, second.shuffle_questions) == (40, True)


# --- структура й завантаження -------------------------------------------


def test_unknown_key_is_content_error():
    def mutate(d):
        q_choice(d)["todoo"] = "друкарська помилка"

    with pytest.raises(ContentError, match="todoo"):
        parse_content(make(mutate))


def test_missing_required_field_is_content_error():
    def mutate(d):
        del q_choice(d)["text"]

    with pytest.raises(ContentError, match="text"):
        parse_content(make(mutate))


def test_unknown_question_type_is_content_error():
    def mutate(d):
        d["rounds"][0]["sections"][0]["question_type"] = "essay"

    with pytest.raises(ContentError):
        parse_content(make(mutate))


def test_load_from_file_and_missing_file(tmp_path: Path):
    path = tmp_path / "quest.yaml"
    path.write_text(yaml.safe_dump(VALID, allow_unicode=True), encoding="utf-8")
    assert load_content(path).question_count == 2
    content, report = load_and_validate(path)
    assert report.issues == [] and content.question_count == 2

    with pytest.raises(ContentError):
        load_content(tmp_path / "немає.yaml")


def test_broken_yaml_is_content_error(tmp_path: Path):
    path = tmp_path / "quest.yaml"
    path.write_text("rounds: [unclosed", encoding="utf-8")
    with pytest.raises(ContentError, match="YAML"):
        load_content(path)


# --- справжній контент ---------------------------------------------------


def test_real_quest_yaml_has_no_errors():
    """Структура справжнього quest.yaml коректна; незаповнене має бути лише як TODO."""
    _, report = load_and_validate(PROJECT_ROOT / "content" / "quest.yaml")
    assert report.errors == [], report.render()
