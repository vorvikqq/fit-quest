"""Живе табло (DESIGN §11, FR-27): aiohttp-сервер у тому ж процесі, що й бот.

* `GET /board?token=…`      HTML-сторінка для показу екрана в Zoom;
* `GET /api/board?token=…`  JSON, який сторінка опитує кожні 3 с;
* `GET /healthz`            без токена, для перевірки живості.

Токен обов'язковий: порожній `BOARD_TOKEN` вимикає табло. Імена в публічних даних
скорочуються до «Ім'я П.» (DESIGN §11).
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo
from bot.services.stats import collect_stats

log = logging.getLogger(__name__)

BOARD_HTML = Path(__file__).with_name("board.html")
TOP_N = 10
CACHE_TTL = 1.0  # с: захист БД від частих опитувань (кілька відкритих вкладок)

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex",
    "Referrer-Policy": "no-referrer",  # токен лежить в URL
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; connect-src 'self'; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'",
}


def short_name(full_name: str) -> str:
    """«Іван Петренко» → «Іван П.»; одне слово лишається як є."""
    parts = full_name.split()
    if len(parts) < 2:
        return parts[0] if parts else ""
    return f"{parts[0]} {parts[1][0].upper()}."


def format_time(ms: int) -> str:
    seconds = ms / 1000
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


class BoardData:
    """Знімок для табло з крихітним кешем."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        content_provider: Callable[[], Content],
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._content = content_provider
        self._clock = clock
        self._now = now
        self._cached: tuple[float, dict] | None = None

    async def snapshot(self) -> dict:
        ts = self._clock()
        if self._cached and ts - self._cached[0] < CACHE_TTL:
            return self._cached[1]
        async with self._session_factory() as session:
            state = await repo.get_game_state(session)
            stats = await collect_stats(session, hardest_limit=0)
            rows = await repo.leaderboard(session, limit=TOP_N)
        content = self._content()
        data = {
            "title": content.meta.title,
            "phase": state.phase,
            "registration_open": state.registration_open,
            "registered": stats.registered,
            "playing": stats.in_progress,
            "finished": stats.finished,
            "closed_unfinished": stats.closed_unfinished,
            "total_questions": content.question_count,
            "closes_at": state.closes_at.isoformat() if state.closes_at else None,
            "server_time": self._now().isoformat(),
            "top": [
                {
                    "place": r.place,
                    "name": short_name(r.full_name),
                    "score": r.score,
                    "time": format_time(r.total_time_ms),
                }
                for r in rows
            ],
        }
        self._cached = (ts, data)
        return data


def _token_ok(request: web.Request, expected: str) -> bool:
    given = request.query.get("token", "")
    return hmac.compare_digest(given.encode(), expected.encode())


@web.middleware
async def _headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    for key, value in SECURITY_HEADERS.items():
        response.headers[key] = value
    if isinstance(response, web.HTTPException):
        raise response
    return response


def create_app(board: BoardData, token: str) -> web.Application:
    if not token:
        raise ValueError("BOARD_TOKEN порожній: табло без токена не запускаємо")
    html = BOARD_HTML.read_text(encoding="utf-8")

    async def page(request: web.Request) -> web.Response:
        if not _token_ok(request, token):
            raise web.HTTPForbidden(text="Forbidden")
        return web.Response(text=html, content_type="text/html")

    async def api(request: web.Request) -> web.Response:
        if not _token_ok(request, token):
            raise web.HTTPForbidden(text="Forbidden")
        return web.json_response(await board.snapshot())

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(middlewares=[_headers])
    app.add_routes(
        [web.get("/board", page), web.get("/api/board", api), web.get("/healthz", healthz)]
    )
    return app


async def start_board(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)  # токен в URL: не пишемо запити в лог
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Табло: порт %d (/board?token=…)", port)
    return runner
