"""`/reload_content`: безпечне перечитування quest.yaml без рестарту (DESIGN §10, §13)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.loader import ContentError, Report, file_sha256, load_and_validate
from bot.content.models import Content
from bot.db import repo
from bot.db.models import Phase


class ReloadRefused(Exception):
    """Перечитати не можна; поточний контент лишається. Текст призначений для адміна."""


@dataclass(frozen=True)
class Reloaded:
    content: Content
    report: Report
    content_hash: str
    changed: bool


async def load_for_reload(path: Path, session_factory: async_sessionmaker[AsyncSession]) -> Reloaded:
    """Читає й перевіряє файл, а потім вирішує, чи можна підміняти ним поточний контент."""
    try:
        content, report = load_and_validate(path)
        new_hash = file_sha256(path)
    except (ContentError, OSError) as exc:
        raise ReloadRefused(f"Файл не прочитано: {exc}") from exc

    if report.errors:
        shown = "; ".join(f"{i.where}: {i.message}" for i in report.errors[:5])
        raise ReloadRefused(f"У контенті {len(report.errors)} помилок, лишаю попередню версію. {shown}")

    async with session_factory() as session:
        state = await repo.get_game_state(session)
        used_qids = await repo.list_plan_qids(session)

    changed = new_hash != state.content_hash
    if state.phase == Phase.RUNNING and state.content_hash and changed:
        raise ReloadRefused(
            "Квест іде, а файл змінився з моменту старту: перечитувати заборонено (DESIGN §10). "
            "Поверни файл або зачекай на /close_quest."
        )

    # Спроби посилаються на `qid`: якщо питання зникло, розбір результатів зламається.
    new_qids = {q.id for _, _, q in content.iter_questions()}
    missing = sorted(used_qids - new_qids)
    if missing:
        raise ReloadRefused(
            f"У нових даних немає питань, які вже є в спробах: {', '.join(missing[:5])}"
            + ("…" if len(missing) > 5 else "")
        )
    return Reloaded(content, report, new_hash, changed)
