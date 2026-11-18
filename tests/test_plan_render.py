from __future__ import annotations

import random
from collections import Counter

import pytest

from bot.config import PROJECT_ROOT
from bot.content.loader import load_content
from bot.content.textutil import html_problems
from bot.services.plan import ContentIndex, build_plan
from bot.services.render import CAPTION_LIMIT, render_card, render_question
from tests.factory import make_content


@pytest.fixture(scope="module")
def real():
    return load_content(PROJECT_ROOT / "content" / "quest.yaml")


# --- build_plan ---------------------------------------------------------


def test_plan_contains_every_question_exactly_once():
    content = make_content()
    plan = build_plan(content, random.Random(1))
    assert sorted(e["qid"] for e in plan) == ["q1", "q2", "q3", "q4", "q5"]


def test_plan_keeps_rounds_and_sections_in_order():
    content = make_content()
    index = ContentIndex(content)
    for seed in range(20):
        plan = build_plan(content, random.Random(seed))
        sections = [index.info(e["qid"]).section.id for e in plan]
        assert sections == ["s1", "s1", "s2a", "s2a", "s2b"]


def test_option_order_is_permutation_and_binary_is_fixed():
    content = make_content()
    seen_orders = set()
    for seed in range(40):
        plan = {e["qid"]: e["option_order"] for e in build_plan(content, random.Random(seed))}
        assert sorted(plan["q1"]) == [0, 1, 2, 3] and sorted(plan["q2"]) == [0, 1, 2]
        assert plan["q3"] == [0, 1] and plan["q4"] == [0, 1]  # FR-10: мітки не перемішуємо
        seen_orders.add(tuple(plan["q1"]))
    assert len(seen_orders) > 5  # порядок справді різний


def test_shuffle_options_false_keeps_original_order():
    content = make_content()
    content.rounds[0].shuffle_options = False
    plan = build_plan(content, random.Random(3))
    assert plan[0]["option_order"] == [0, 1, 2, 3]


def test_shuffle_questions_per_section():
    content = make_content()
    content.rounds[1].shuffle_questions = True
    orders = {tuple(e["qid"] for e in build_plan(content, random.Random(s))[2:4]) for s in range(30)}
    assert orders == {("q3", "q4"), ("q4", "q3")}
    # перша секція без shuffle_questions
    plan = build_plan(content, random.Random(5))
    assert [e["qid"] for e in plan[:2]] == ["q1", "q2"]


def test_plan_is_seeded_and_json_friendly():
    content = make_content()
    assert build_plan(content, random.Random(7)) == build_plan(content, random.Random(7))
    import json

    json.dumps(build_plan(content, random.Random(7)))


def test_real_content_plan_covers_every_question(real):
    plan = build_plan(real, random.Random(0))
    assert len(plan) == real.question_count == 50 and len({e["qid"] for e in plan}) == 50
    # раунд 2 і 3 — з перемішаними питаннями, раунд 1 — ні
    assert [e["qid"] for e in plan[:10]] == [f"r1q{i:02d}" for i in range(1, 11)]


# --- boundaries / positions ---------------------------------------------


def test_boundaries_and_positions():
    content = make_content()
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    assert [index.boundary_at(plan, i) for i in range(5)] == ["round", None, "round", None, "section"]
    assert [index.position_in_round(plan, i) for i in range(5)] == [
        (1, 2), (2, 2), (1, 3), (2, 3), (3, 3),
    ]


# --- render -------------------------------------------------------------


def test_question_header_and_choice_layout():
    content = make_content()
    index = ContentIndex(content)
    plan = [{"qid": "q1", "option_order": [3, 1, 0, 2]}] + build_plan(content)[1:]
    view = render_question(index, plan, 0)
    lines = view.text.split("\n")
    assert lines[0] == "Раунд 1/2 · Питання 1/2 · ⏱ 10 с"
    # варіанти показані в персональному порядку: d, b, a, c
    assert [ln.split(" ", 1)[1] for ln in lines[-4:]] == ["d", "b", "a", "c"]
    assert [b[0] for b in view.buttons] == ["A", "B", "C", "D"]
    assert not view.split and view.image is None


def test_binary_buttons_use_labels():
    content = make_content()
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    view = render_question(index, plan, 2)  # q3, binary з картинкою
    assert [b[0] for b in view.buttons] == ["Так", "Ні"]
    assert view.image == "images/r5_photo_01.png"
    assert view.caption_mode and not view.split


def test_long_caption_with_image_is_split():
    """DESIGN §6.3: підпис > 1024 → картинка окремо, питання з кнопками наступним."""
    content = make_content()
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    view = render_question(index, plan, 4)  # q5: 4 довгі варіанти + картинка
    assert view.split and view.image and not view.caption_mode
    assert len(view.buttons) == 4


def test_options_are_html_escaped():
    content = make_content()
    content.rounds[0].sections[0].questions[0].options = ["<<", "#include <cmath>", "a & b", ">>"]
    index = ContentIndex(content)
    plan = [{"qid": "q1", "option_order": [0, 1, 2, 3]}] + build_plan(content)[1:]
    view = render_question(index, plan, 0)
    assert "&lt;&lt;" in view.text and "#include &lt;cmath&gt;" in view.text and "a &amp; b" in view.text
    assert html_problems(view.text) == []


def test_every_real_question_renders_to_valid_html(real):
    index = ContentIndex(real)
    plan = build_plan(real, random.Random(0))
    for cursor in range(len(plan)):
        view = render_question(index, plan, cursor)
        assert html_problems(view.text) == [], plan[cursor]["qid"]
        assert len(view.buttons) in (2, 4)


def test_real_r5_photo_and_prompt_questions_fit_in_caption(real):
    """У фінальному контенті промпти короткі: усі фото йдуть підписом, розбиття не потрібне.

    Випадок «підпис > 1024» лишається покритим синтетичним `test_long_caption_with_image_is_split`.
    """
    index = ContentIndex(real)
    plan = build_plan(real, random.Random(0))
    with_images = [i for i, e in enumerate(plan) if index.info(e["qid"]).question.image]
    assert len(with_images) == 10
    for cursor in with_images:
        view = render_question(index, plan, cursor)
        assert view.caption_mode and not view.split, plan[cursor]["qid"]


def test_real_round5_has_two_sections_and_numbers_come_from_content(real):
    """Нічого не прописано жорстко: «Раунд X/5 · Питання Y/Z» і картки беруться з контенту."""
    index = ContentIndex(real)
    plan = build_plan(real, random.Random(0))
    r5 = real.rounds[4]
    assert [s.id for s in r5.sections] == ["r5s2", "r5s3"]
    size = sum(len(s.questions) for s in r5.sections)
    assert size == 10 and index.round_size("r5") == size

    first = next(i for i, e in enumerate(plan) if index.info(e["qid"]).round.id == "r5")
    last = len(plan) - 1
    assert index.position_in_round(plan, first) == (1, size)
    assert index.position_in_round(plan, last) == (size, size)
    assert f"Раунд 5/{len(real.rounds)} · Питання 1/{size}" in render_question(index, plan, first).text
    assert f"Раунд 5/{len(real.rounds)} · Питання {size}/{size}" in render_question(index, plan, last).text

    # картка раунду 5 — про фото (перша секція), а друга секція отримує власну картку
    card = render_card(index, plan, first)
    assert "Раунд 5" in card and f"Питань: {len(r5.sections[0].questions)}" in card
    second = first + len(r5.sections[0].questions)
    assert index.boundary_at(plan, second) == "section"
    assert f"Питань: {len(r5.sections[1].questions)}" in render_card(index, plan, second)


def test_header_and_cards_follow_content_when_a_section_is_removed():
    """Прибрали секцію з контенту: нумерація й підсумки змінюються самі, без правок коду."""
    content = make_content()
    full_index = ContentIndex(content)
    assert full_index.round_size("r2") == 3
    content.rounds[1].sections.pop()  # лишається лише s2a: 2 питання
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    assert len(plan) == 4 and index.round_size("r2") == 2
    assert "Питання 2/2" in render_question(index, plan, 3).text
    assert "Раунд 2/2" in render_question(index, plan, 2).text


def test_split_threshold_counts_utf16_units():
    from bot.content.textutil import plain, tg_len

    content = make_content()
    q = content.rounds[1].sections[0].questions[0]  # q3: binary + image
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    q.text = "😀" * 520  # 1040 UTF-16 units при 520 символах
    assert tg_len(plain(q.text)) > CAPTION_LIMIT
    assert render_question(index, plan, 2).split


# --- картки -------------------------------------------------------------


def test_round_card_has_title_intro_and_stats():
    content = make_content()
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    card = render_card(index, plan, 0)
    assert "<b>Раунд Один</b>" in card and "вступ раунду 1" in card
    assert "Питань: 2 · ⏱ 10 с на кожне" in card


def test_round_card_includes_first_section_intro_and_section_card_does_not_repeat_title():
    content = make_content()
    index = ContentIndex(content)
    plan = build_plan(content, random.Random(0))
    round2 = render_card(index, plan, 2)
    assert "Раунд Два" in round2 and "вступ секції 2a" in round2 and "⏱ 5 с" in round2
    section = render_card(index, plan, 4)
    assert "вступ секції 2b" in section and "Раунд Два" not in section
    assert "Питань: 1 · ⏱ 7 с" in section  # власний time_limit секції


def test_stat_counter_helper_not_vacuous():
    # захист від випадкового «все зелене»: лічильник працює
    assert Counter("aab")["a"] == 2
