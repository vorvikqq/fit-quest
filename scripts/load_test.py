"""Навантажувальний тест ігрового ядра (DESIGN §15.2): N «учасників» без Telegram.

Віртуальні учасники працюють через `services/game.py` так само, як хендлери: тиснуть
«Почати квест», «Почати раунд» і відповідають на питання, реагуючи на повідомлення
підробленого месенджера. Двигун БД створюється тим самим `make_engine`, що й у проді, тож
пул з'єднань і блокування Postgres впливають на результат так само.

ПОПЕРЕДЖЕННЯ: скрипт **стирає** учасників і спроби в БД. Тому за замовчуванням він працює
лише з локальною БД і відмовляється ходити на віддалений хост чи на сервіс `db` з compose.

Приклад (тимчасовий Postgres, див. CLAUDE.md):
    docker run -d --name fq-test-pg -e POSTGRES_USER=quest -e POSTGRES_PASSWORD=quest \\
        -e POSTGRES_DB=quest -p 127.0.0.1:55432:5432 postgres:16
    python scripts/load_test.py --players 200 --think-ms 800

`--think-ms` — середня «пауза на роздуми» між питаннями в мілісекундах. У реальності ~8000
(DESIGN §9: 200 людей по 1 відповіді на 8 с ≈ 25 повідомлень/с). За замовчуванням час
стиснуто в 10 разів, щоб прогін тривав хвилину, а не вісім: навантаження на секунду вище
за реальне, тож тест консервативний. Для точної імітації: `--think-ms 8000`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402

from bot.content.loader import load_content  # noqa: E402
from bot.db import repo  # noqa: E402
from bot.db.models import Answer, Attempt, AttemptStatus, User  # noqa: E402
from bot.db.session import make_engine, make_session_factory  # noqa: E402
from bot.services.game import GameService  # noqa: E402

DEFAULT_URL = os.environ.get(
    "LOAD_TEST_DATABASE_URL", "postgresql+asyncpg://quest:quest@127.0.0.1:55432/quest"
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BASE_ID = 9_000_000  # діапазон tg_id віртуальних учасників


@dataclass
class Event:
    kind: str  # card | question | done
    cursor: int = 0
    buttons: int = 0


class NullMessenger:
    """Не шле нічого, лише будить віртуальних учасників; затримку Telegram можна змоделювати."""

    def __init__(self, latency: float = 0.0) -> None:
        self.latency = latency
        self.inbox: dict[int, asyncio.Queue[Event]] = {}
        self._next_id = 0
        self.sent = 0

    def queue(self, tg_id: int) -> asyncio.Queue[Event]:
        return self.inbox.setdefault(tg_id, asyncio.Queue())

    async def _api(self) -> int:
        self.sent += 1
        if self.latency:
            await asyncio.sleep(self.latency)
        self._next_id += 1
        return self._next_id

    async def send_text(self, tg_id, text):
        return await self._api()

    async def send_card(self, tg_id, text, cursor):
        msg = await self._api()
        self.queue(tg_id).put_nowait(Event("card", cursor))
        return msg

    async def send_question(self, tg_id, view, cursor):
        msg = await self._api()
        self.queue(tg_id).put_nowait(Event("question", cursor, len(view.buttons)))
        return msg

    async def send_feedback_prompt(self, tg_id, text):
        msg = await self._api()
        self.queue(tg_id).put_nowait(Event("done"))
        return msg

    async def close_question(self, tg_id, message_id, view, suffix):
        await self._api()

    async def remove_keyboard(self, tg_id, message_id):
        await self._api()


@dataclass
class Metrics:
    latencies: dict[str, list[float]] = field(default_factory=dict)
    stale_ok: int = 0  # дублі, які сервер правильно відхилив
    stale_bad: int = 0  # дублі, які зарахувалися двічі (мусить бути 0)
    errors: list[str] = field(default_factory=list)

    def add(self, name: str, seconds: float) -> None:
        self.latencies.setdefault(name, []).append(seconds)

    def all(self) -> list[float]:
        return [x for values in self.latencies.values() for x in values]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


async def timed(metrics: Metrics, name: str, coro):
    start = time.perf_counter()
    try:
        return await coro
    finally:
        metrics.add(name, time.perf_counter() - start)


async def player(
    uid: int,
    game: GameService,
    messenger: NullMessenger,
    metrics: Metrics,
    rng: random.Random,
    think: float,
    double_click: float,
) -> None:
    inbox = messenger.queue(uid)
    try:
        await timed(metrics, "start", game.start_or_resume(uid))
        while True:
            event = await asyncio.wait_for(inbox.get(), timeout=120)
            if event.kind == "done":
                return
            if event.kind == "card":
                await timed(metrics, "go", game.go(uid, event.cursor))
                continue
            await asyncio.sleep(rng.expovariate(1 / think) if think else 0)  # «читає питання»
            btn = rng.randrange(event.buttons)
            if rng.random() < double_click:  # нервове подвійне натискання
                first, second = await asyncio.gather(
                    timed(metrics, "answer", game.answer(uid, event.cursor, btn)),
                    timed(metrics, "answer_dup", game.answer(uid, event.cursor, btn)),
                )
                if first and second:
                    metrics.stale_bad += 1
                else:
                    metrics.stale_ok += 1
            else:
                await timed(metrics, "answer", game.answer(uid, event.cursor, btn))
    except Exception as exc:  # noqa: BLE001
        metrics.errors.append(f"{uid}: {type(exc).__name__}: {exc}")


async def verify(session_factory, players: int, total_questions: int) -> list[str]:
    """Інваріанти після прогону: усі дійшли до кінця, ніхто не зарахований двічі."""
    problems: list[str] = []
    async with session_factory() as session:
        attempts = list(
            (await session.execute(select(Attempt).where(Attempt.tg_id >= BASE_ID))).scalars()
        )
        if len(attempts) != players:
            problems.append(f"спроб {len(attempts)}, очікували {players}")
        unfinished = [a.tg_id for a in attempts if a.status != AttemptStatus.FINISHED]
        if unfinished:
            problems.append(f"не завершили {len(unfinished)}: {unfinished[:5]}")

        counts = dict(
            (
                await session.execute(
                    select(Answer.attempt_id, func.count()).group_by(Answer.attempt_id)
                )
            ).all()
        )
        wrong_count = [a.tg_id for a in attempts if counts.get(a.id, 0) != total_questions]
        if wrong_count:
            problems.append(f"невірна кількість відповідей у {len(wrong_count)}: {wrong_count[:5]}")

        correct = dict(
            (
                await session.execute(
                    select(Answer.attempt_id, func.count()).where(Answer.is_correct.is_(True)).group_by(Answer.attempt_id)
                )
            ).all()
        )
        wrong_score = [a.tg_id for a in attempts if a.score != correct.get(a.id, 0)]
        if wrong_score:
            problems.append(f"бал не збігається з відповідями у {len(wrong_score)}: {wrong_score[:5]}")
    return problems


def guard_database(url: str, allow_remote: bool) -> None:
    host = urlparse(url.replace("+asyncpg", "")).hostname or ""
    if host not in LOCAL_HOSTS and not allow_remote:
        raise SystemExit(
            f"Відмова: хост БД «{host}» не локальний. Скрипт стирає дані! "
            "Використай окрему тестову БД або додай --allow-remote, якщо впевнений."
        )


async def prepare_database(url: str, session_factory, players: int) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "bot" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    await asyncio.to_thread(command.upgrade, cfg, "head")  # env.py сам крутить свій event loop

    async with session_factory.begin() as session:
        await repo.ensure_game_state(session, "LOAD")
        await repo.reset_event(session)
        await repo.update_game_state(session, phase="RUNNING", registration_open=False)
        now = datetime.now(UTC)
        session.add_all(
            User(
                tg_id=BASE_ID + i,
                username=f"load{i}",
                tg_name=f"Load {i}",
                full_name=f"Тест Учасник{i}",
                status="OTHER",
                registered_at=now,
            )
            for i in range(players)
        )


async def run(args: argparse.Namespace) -> int:
    guard_database(args.database_url, args.allow_remote)
    content = load_content(args.content)
    engine = make_engine(args.database_url)  # той самий двигун і пул, що в проді
    session_factory = make_session_factory(engine)
    try:
        await prepare_database(args.database_url, session_factory, args.players)
        messenger = NullMessenger(latency=args.telegram_latency_ms / 1000)
        game = GameService(session_factory, content, messenger, rng=random.Random(args.seed))
        metrics = Metrics()
        rng = random.Random(args.seed)

        print(
            f"Учасників: {args.players}, питань: {content.question_count}, "
            f"пауза ~{args.think_ms} мс, затримка Telegram {args.telegram_latency_ms} мс, "
            f"подвійні натискання {args.double_click:.0%}"
        )
        started = time.perf_counter()
        await asyncio.gather(
            *(
                player(
                    BASE_ID + i, game, messenger, metrics, random.Random(rng.random()),
                    args.think_ms / 1000, args.double_click,
                )
                for i in range(args.players)
            )
        )
        wall = time.perf_counter() - started
        game.scheduler.cancel_all()

        problems = await verify(session_factory, args.players, content.question_count)
        report = summarize(metrics, wall, messenger, args)
        print(report.text)
        problems += report.problems
        problems += [f"помилка учасника {e}" for e in metrics.errors[:10]]
        if metrics.stale_bad:
            problems.append(f"{metrics.stale_bad} подвійних натискань зараховано двічі!")

        if problems:
            print("\nПРОВАЛ:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print("\nПРОЙДЕНО: усі завершили, дублів немає, затримка в межах NFR-2.")
        return 0
    finally:
        await engine.dispose()


@dataclass
class Report:
    text: str
    problems: list[str]


def summarize(metrics: Metrics, wall: float, messenger: NullMessenger, args) -> Report:
    lines = [f"\nЧас прогону: {wall:.1f} с, API-викликів «Telegram»: {messenger.sent}"]
    lines.append(f"{'виклик':<12}{'кількість':>10}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}  (мс)")
    for name in ("start", "go", "answer", "answer_dup"):
        values = metrics.latencies.get(name)
        if not values:
            continue
        ms = lambda v: f"{v * 1000:.0f}"  # noqa: E731
        lines.append(
            f"{name:<12}{len(values):>10}{ms(statistics.median(values)):>9}"
            f"{ms(percentile(values, .95)):>9}{ms(percentile(values, .99)):>9}{ms(max(values)):>9}"
        )
    answers = metrics.latencies.get("answer", [])
    problems = []
    all_p95 = percentile(metrics.all(), 0.95)
    # Затримка Telegram у модель входить у кожен виклик; NFR-2 стосується відповіді бота загалом.
    if answers and percentile(answers, 0.95) > args.max_p95:
        problems.append(
            f"p95 відповіді {percentile(answers, 0.95) * 1000:.0f} мс > порогу {args.max_p95 * 1000:.0f} мс (NFR-2)"
        )
    lines.append(f"Загальний p95: {all_p95 * 1000:.0f} мс; відповідей/с: {len(answers) / wall:.1f}")
    lines.append(f"Подвійні натискання: відхилено правильно {metrics.stale_ok}, зараховано двічі {metrics.stale_bad}")
    return Report("\n".join(lines), problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--players", type=int, default=200)
    parser.add_argument("--think-ms", type=float, default=800, help="середня пауза між питаннями")
    parser.add_argument("--telegram-latency-ms", type=float, default=0, help="імітація затримки Telegram API")
    parser.add_argument("--double-click", type=float, default=0.05, help="частка подвійних натискань")
    parser.add_argument("--max-p95", type=float, default=1.0, help="поріг p95 відповіді, с (NFR-2)")
    parser.add_argument("--database-url", default=DEFAULT_URL)
    parser.add_argument("--allow-remote", action="store_true", help="дозволити нелокальну БД (стирає дані!)")
    parser.add_argument("--content", type=Path, default=ROOT / "content" / "quest.yaml")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
