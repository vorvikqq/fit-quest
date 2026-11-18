"""Робота з текстом Telegram: довжина після розбору HTML, перевірка розмітки."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_TAG_NAME_RE = re.compile(r"^</?\s*([a-zA-Z][\w-]*)")

# Теги, які дозволяє parse_mode=HTML у Telegram.
ALLOWED_TAGS = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler", "blockquote"}
)


def plain(text: str) -> str:
    """Те, що Telegram порахує в ліміті: без HTML-тегів, сутності розкриті."""
    return html.unescape(_TAG_RE.sub("", text))


def tg_len(text: str) -> int:
    """Довжина в UTF-16 code units (так Telegram рахує ліміти; емодзі = 2)."""
    return len(text.encode("utf-16-le")) // 2


def html_problems(text: str) -> list[str]:
    """Проблеми, через які Telegram відхилить текст у parse_mode=HTML.

    Перевіряє невідомі теги, «голі» `<`/`>` і незакриті теги. `&` не перевіряє.
    """
    problems: list[str] = []
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        tag = match.group(0)
        name_match = _TAG_NAME_RE.match(tag)
        if not name_match or name_match.group(1).lower() not in ALLOWED_TAGS:
            problems.append(f"недозволений тег {tag!r}")
            continue
        name = name_match.group(1).lower()
        if tag.startswith("</"):
            if not stack or stack[-1] != name:
                problems.append(f"зайвий закриваючий тег {tag!r}")
            else:
                stack.pop()
        else:
            stack.append(name)
    problems.extend(f"не закрито тег <{name}>" for name in stack)

    leftover = _TAG_RE.sub("", text)
    if "<" in leftover or ">" in leftover:
        problems.append("«голий» символ < або > (треба &lt; / &gt;)")
    return problems
