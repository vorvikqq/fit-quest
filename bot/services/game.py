"""Ядро гри (DESIGN §6.1–6.5, §7).

Стан живе в таблиці `attempts`. Прийом відповіді й таймаут — один і той самий атомарний
`UPDATE … WHERE cursor = :cursor AND status = 'QUESTION'` (`repo.advance_attempt`):
хто перший змінив `cursor`, той і зарахований. Це не послаблювати.

Стан «питання треба надіслати» = `status=QUESTION`, `q_sent_at IS NULL`. Надсилає його
той, хто перевів спробу в цей стан (переможець CAS або натискання «Почати раунд»);
після падіння бота його підхоплює recovery.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.db.models import Attempt, AttemptStatus, Phase
from bot.services.messenger import Messenger
from bot.services.plan import ContentIndex, build_plan
from bot.services.render import render_card, render_question
from bot.services.timers import TimerScheduler

log = logging.getLogger(__name__)

# Допуск на мережу (NFR-3): відповідь, що прийшла до `limit + GRACE`, ще рахується.
GRACE = timedelta(milliseconds=1500)

StartResult = Literal["started", "resumed", "not_registered", "not_started", "closed"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class GameService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        content: Content,
        messenger: Messenger,
        *,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        scheduler: TimerScheduler | None = None,
        grace: timedelta = GRACE,
    ) -> None:
        self.session_factory = session_factory
        self.content = content
        self.index = ContentIndex(content)
        self.messenger = messenger
        self.clock = clock
        self.rng = rng
        self.scheduler = scheduler or TimerScheduler()
        self.grace = grace

    def set_content(self, content: Content) -> None:
        """Гаряче підміняє контент (`/reload_content`). Спроби посилаються лише на `qid`."""
        self.content = content
        self.index = ContentIndex(content)
        if hasattr(self.messenger, "content"):
            self.messenger.content = content  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Старт і відновлення (§6.1, §3.2)
    # ------------------------------------------------------------------

    async def start_or_resume(self, tg_id: int) -> StartResult:
        """Кнопка «Почати квест» або `/start` зареєстрованого: стартує чи повертає в гру."""
        created = False
        async with self.session_factory.begin() as session:
            user = await repo.get_user(session, tg_id)
            state = await repo.get_game_state(session)
            attempt = await repo.get_attempt_for_user(session, tg_id)
            if attempt is None:
                if user is None or user.registered_at is None:
                    return "not_registered"
                if state.phase != Phase.RUNNING:
                    return "not_started" if state.phase in (Phase.DRAFT, Phase.REGISTRATION) else "closed"
                plan = build_plan(self.content, self.rng)
                attempt = await repo.create_attempt(session, tg_id, plan, self.clock())
                created = attempt is not None
                if attempt is None:  # паралельний старт того ж акаунта: беремо вже створену
                    attempt = await repo.get_attempt_for_user(session, tg_id)

        if created:
            await self.messenger.send_text(tg_id, self.content.messages["rules"])
            if state.closes_at is not None:  # FR-25: скільки лишилось до закриття
                minutes = max(1, round((state.closes_at - self.clock()).total_seconds() / 60))
                await self.messenger.send_text(
                    tg_id, self.content.messages["time_left"].format(minutes=minutes)
                )
            await self._send_card(attempt)
            return "started"

        await self._resume(attempt)
        return "resumed"

    async def _resume(self, attempt: Attempt) -> None:
        """Повертає учасника туди, де він зупинився."""
        status = attempt.status
        if status == AttemptStatus.ROUND_INTRO:
            await self._send_card(attempt)
        elif status == AttemptStatus.QUESTION:
            if attempt.q_sent_at is None:
                await self.send_current_question(attempt)
            else:
                await self._resend_question(attempt)
        else:
            await self._send_terminal(attempt)

    async def _send_terminal(self, attempt: Attempt) -> None:
        total = len(attempt.plan)
        if attempt.status == AttemptStatus.FINISHED:
            text = self.content.messages["finished"].format(score=attempt.score, total=total)
        else:
            answered = attempt.cursor
            text = self.content.messages["quest_closed_unfinished"].format(
                answered=answered, total=total, score=attempt.score
            )
        await self.messenger.send_text(attempt.tg_id, text)

    # ------------------------------------------------------------------
    # Картки й питання (§6.2, §6.3)
    # ------------------------------------------------------------------

    async def _send_card(self, attempt: Attempt) -> None:
        text = render_card(self.index, attempt.plan, attempt.cursor)
        await self.messenger.send_card(attempt.tg_id, text, attempt.cursor)

    async def go(self, tg_id: int, cursor: int) -> bool:
        """«▶️ Почати раунд». `False`, якщо кнопка застаріла чи це дубль."""
        async with self.session_factory.begin() as session:
            attempt = await repo.get_attempt_for_user(session, tg_id)
            if attempt is None or attempt.cursor != cursor or attempt.status != AttemptStatus.ROUND_INTRO:
                return False
            # Claim: лише один з подвійних кліків перейде в QUESTION й надішле питання.
            claimed = await repo.set_attempt_status(
                session, attempt.id, AttemptStatus.ROUND_INTRO, AttemptStatus.QUESTION
            )
        if not claimed:
            return False
        await self.send_current_question(attempt)
        return True

    async def send_current_question(self, attempt: Attempt) -> None:
        """Надсилає питання на `attempt.cursor` і ставить дедлайн (§6.3, кроки 1–4)."""
        cursor = attempt.cursor
        view = render_question(self.index, attempt.plan, cursor)
        message_id = await self.messenger.send_question(attempt.tg_id, view, cursor)

        info = self.index.info(attempt.plan[cursor]["qid"])
        limit = timedelta(seconds=info.settings.time_limit)
        sent_at = self.clock()
        async with self.session_factory.begin() as session:
            marked = await repo.mark_question_sent(
                session, attempt.id, cursor, sent_at, sent_at + limit, message_id
            )
        if marked:
            self.scheduler.schedule(
                attempt.id,
                (limit + self.grace).total_seconds(),
                partial(self.on_timeout, attempt.id, cursor),
            )

    async def _resend_question(self, attempt: Attempt) -> None:
        """Повторне надсилання поточного питання з тим самим дедлайном (§3.2)."""
        cursor = attempt.cursor
        view = render_question(self.index, attempt.plan, cursor)
        new_id = await self.messenger.send_question(attempt.tg_id, view, cursor)
        async with self.session_factory.begin() as session:
            if new_id is not None:
                await repo.set_question_message_id(session, attempt.id, cursor, new_id)
        await self.messenger.remove_keyboard(attempt.tg_id, attempt.q_message_id)
        self.arm_timer(attempt)

    def arm_timer(self, attempt: Attempt) -> None:
        """Планує таймер за збереженим дедлайном (recovery, повторне надсилання)."""
        if attempt.q_deadline is None:
            return
        remaining = (attempt.q_deadline + self.grace - self.clock()).total_seconds()
        self.scheduler.schedule(
            attempt.id, remaining, partial(self.on_timeout, attempt.id, attempt.cursor)
        )

    # ------------------------------------------------------------------
    # Прийом відповіді й таймаут (§6.4, §6.5)
    # ------------------------------------------------------------------

    async def answer(self, tg_id: int, cursor: int, btn: int) -> bool:
        """Відповідь `a:{cursor}:{btn}`. `False` — застаріла, дубль або некоректна кнопка."""
        async with self.session_factory() as session:
            snapshot = await repo.get_attempt_for_user(session, tg_id)
        if (
            snapshot is None
            or snapshot.cursor != cursor
            or snapshot.status != AttemptStatus.QUESTION
            or snapshot.q_sent_at is None
        ):
            return False
        now = self.clock()
        limit = timedelta(seconds=self.index.info(snapshot.plan[cursor]["qid"]).settings.time_limit)
        late = now - snapshot.q_sent_at > limit + self.grace  # NFR-3
        return await self._resolve(snapshot, btn=btn, timed_out=late, now=now)

    async def on_timeout(self, attempt_id: int, cursor: int) -> bool:
        """Таймер, сторож і recovery. Та сама CAS, що й у `answer`."""
        async with self.session_factory() as session:
            snapshot = await repo.get_attempt(session, attempt_id)
        if (
            snapshot is None
            or snapshot.cursor != cursor
            or snapshot.status != AttemptStatus.QUESTION
            or snapshot.q_sent_at is None
        ):
            return False
        return await self._resolve(snapshot, btn=None, timed_out=True, now=self.clock())

    async def _resolve(
        self, snapshot: Attempt, *, btn: int | None, timed_out: bool, now: datetime
    ) -> bool:
        cursor = snapshot.cursor
        plan = snapshot.plan
        entry = plan[cursor]
        info = self.index.info(entry["qid"])
        limit_ms = info.settings.time_limit * 1000

        chosen: int | None = None
        is_correct = False
        response_ms = limit_ms
        if btn is not None and not timed_out:
            order = entry["option_order"]
            if not 0 <= btn < len(order):
                return False
            chosen = order[btn]  # індекс в ОРИГІНАЛЬНОМУ порядку з yaml
            is_correct = info.question.correct is not None and chosen == info.question.correct
            elapsed_ms = int((now - snapshot.q_sent_at).total_seconds() * 1000)
            response_ms = max(0, min(elapsed_ms, limit_ms))

        next_cursor = cursor + 1
        finished_at = None
        if next_cursor >= len(plan):
            next_status, finished_at = AttemptStatus.FINISHED, now
        elif self.index.boundary_at(plan, next_cursor):
            next_status = AttemptStatus.ROUND_INTRO
        else:
            next_status = AttemptStatus.QUESTION

        # Одна транзакція: перехід + запис відповіді. Telegram — лише після коміту.
        async with self.session_factory.begin() as session:
            updated = await repo.advance_attempt(
                session,
                snapshot.id,
                cursor,
                next_status,
                score_delta=int(is_correct),
                time_delta_ms=response_ms,
                finished_at=finished_at,
            )
            if updated is None:
                return False  # відповідь застаріла або дубль (NFR-4)
            await repo.insert_answer(
                session, snapshot.id, entry["qid"], chosen, is_correct, response_ms, now
            )

        self.scheduler.cancel(snapshot.id)
        suffix = self.content.messages["time_is_up" if chosen is None else "answer_accepted"]
        view = render_question(self.index, plan, cursor)
        await self.messenger.close_question(snapshot.tg_id, snapshot.q_message_id, view, suffix)

        if next_status == AttemptStatus.FINISHED:
            await self._finish(updated)
        elif next_status == AttemptStatus.ROUND_INTRO:
            await self._send_card(updated)
        else:
            await self.send_current_question(updated)
        return True

    # ------------------------------------------------------------------
    # Фініш (§6.6) і закриття (§6.7)
    # ------------------------------------------------------------------

    async def _promo_texts(self, attempt: Attempt) -> list[str]:
        """Промокод Hillel + партнери. Лише один раз на спробу (`promo_sent`, FR-19)."""
        async with self.session_factory.begin() as session:
            first = await repo.mark_promo_sent(session, attempt.id)
        if not first:
            return []
        return [self.content.meta.promo.text, self.content.messages["partners"]]

    async def _finish(self, attempt: Attempt) -> None:
        messages = self.content.messages
        await self.messenger.send_text(
            attempt.tg_id, messages["finished"].format(score=attempt.score, total=len(attempt.plan))
        )
        for text in await self._promo_texts(attempt):
            await self.messenger.send_text(attempt.tg_id, text)
        await self.messenger.send_feedback_prompt(attempt.tg_id, messages["ask_feedback"])

    def cancel_timers(self, attempts) -> None:
        for attempt in attempts:
            self.scheduler.cancel(attempt.id)

    async def close_notifications(self, attempts) -> list[tuple[int, str]]:
        """Повідомлення тим, кого закрили посеред гри: «квест закрито» + промокод (FR-19, FR-22)."""
        items: list[tuple[int, str]] = []
        for attempt in attempts:
            items.append(
                (
                    attempt.tg_id,
                    self.content.messages["quest_closed_unfinished"].format(
                        answered=attempt.cursor, total=len(attempt.plan), score=attempt.score
                    ),
                )
            )
            items.extend((attempt.tg_id, text) for text in await self._promo_texts(attempt))
        return items
