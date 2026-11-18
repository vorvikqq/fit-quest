from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.db import repo
from bot.web import server
from bot.web.server import BoardData, create_app, format_time, short_name
from tests.factory import make_content, register_users
from tests.harness import ADMIN_ID, Harness
from tests.test_admin_lifecycle import open_and_start, play
from tests.test_admin_tools import played_event

TOKEN = "t0ken-" + "x" * 20
U1, U2, U3 = 6001, 6002, 6003


async def make_harness(session_factory) -> Harness:
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    return Harness(session_factory, content=make_content())


class Ticker:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def board(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s, "Secret")
    ticker = Ticker()
    data = BoardData(session_factory, make_content, clock=ticker)
    data.ticker = ticker
    return data


@pytest.fixture
async def client(board):
    async with TestClient(TestServer(create_app(board, TOKEN))) as c:
        yield c


# --- чисті функції --------------------------------------------------------


@pytest.mark.parametrize(
    ("full", "short"),
    [
        ("Іван Петренко", "Іван П."),
        ("іван  петренко  іванович", "іван П."),
        ("Оля", "Оля"),
        ("", ""),
        ("  Анна   коваль ", "Анна К."),
    ],
)
def test_short_name(full, short):
    assert short_name(full) == short


def test_format_time():
    assert format_time(0) == "0:00.0"
    assert format_time(83_400) == "1:23.4"
    assert format_time(600_000) == "10:00.0"


# --- доступ ---------------------------------------------------------------


async def test_pages_require_token(client):
    for path in ("/board", "/api/board", "/board?token=", "/api/board?token=wrong", f"/api/board?token={TOKEN}x"):
        assert (await client.get(path)).status == 403, path
    assert (await client.get(f"/board?token={TOKEN}")).status == 200
    assert (await client.get(f"/api/board?token={TOKEN}")).status == 200


async def test_healthz_is_open(client):
    resp = await client.get("/healthz")
    assert resp.status == 200 and await resp.text() == "ok"


async def test_empty_token_refuses_to_start(board):
    with pytest.raises(ValueError, match="BOARD_TOKEN"):
        create_app(board, "")


async def test_security_headers_on_every_response(client):
    for path in (f"/board?token={TOKEN}", f"/api/board?token={TOKEN}", "/board", "/healthz"):
        resp = await client.get(path)
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["X-Robots-Tag"] == "noindex"
        assert "default-src 'none'" in resp.headers["Content-Security-Policy"]


async def test_unknown_route_is_404_not_a_crash(client):
    assert (await client.get("/admin")).status == 404


# --- сторінка -------------------------------------------------------------


async def test_html_is_dark_big_and_polls_every_3_seconds(client):
    html = await (await client.get(f"/board?token={TOKEN}")).text()
    assert "<html lang=\"uk\">" in html
    assert "setInterval(poll, 3000)" in html  # FR-27: оновлення кожні 3 с
    assert "--bg: #0b1020" in html  # темна тема
    assert "textContent" in html and "innerHTML" not in html  # імена ніколи не стають HTML
    assert "/api/board?token=" in html


# --- дані -----------------------------------------------------------------


async def test_api_on_empty_event(client):
    data = await (await client.get(f"/api/board?token={TOKEN}")).json()
    assert data["phase"] == "DRAFT" and data["top"] == [] and data["closes_at"] is None
    assert (data["registered"], data["playing"], data["finished"]) == (0, 0, 0)
    assert data["total_questions"] == 5 and data["title"]


async def test_api_reflects_live_event_with_short_names(session_factory):
    h = await make_harness(session_factory)
    await played_event(h, session_factory)
    board = BoardData(session_factory, lambda: h.content)
    async with TestClient(TestServer(create_app(board, TOKEN))) as client:
        data = await (await client.get(f"/api/board?token={TOKEN}")).json()

    assert data["phase"] == "RUNNING"
    assert (data["registered"], data["playing"], data["finished"]) == (3, 0, 2)
    first, second = data["top"]
    assert (first["place"], first["name"], first["score"]) == (1, "Учасник 6.", 5)  # «Ім'я П.»
    assert (first["score"], second["score"]) == (5, 0)
    assert "6001" not in str(data) and "u6001" not in str(data)  # без id та username


async def test_api_excludes_admins_and_excluded_from_top(session_factory):
    h = await make_harness(session_factory)
    await played_event(h, session_factory)
    async with session_factory.begin() as s:
        await repo.set_user_flag(s, U1, excluded=True)
    board = BoardData(session_factory, lambda: h.content)
    data = await board.snapshot()
    assert [r["score"] for r in data["top"]] == [0]


async def test_api_shows_planned_closing_time(session_factory):
    h = await make_harness(session_factory)
    await open_and_start(h, session_factory, users=(U1,))
    await h.send(ADMIN_ID, "/close_in 15")
    from bot.keyboards import Confirm

    await h.press(ADMIN_ID, Confirm(action="close_in", arg=15, yes=True).pack())
    await h.wait_background()
    data = await BoardData(session_factory, lambda: h.content).snapshot()
    assert data["closes_at"] and data["server_time"] and data["closes_at"] > data["server_time"]


async def test_playing_counter_changes_as_people_start(session_factory):
    h = await make_harness(session_factory)
    await open_and_start(h, session_factory, users=(U1, U2))
    await play(h, session_factory, U1, answers=2)
    data = await BoardData(session_factory, lambda: h.content).snapshot()
    assert (data["registered"], data["playing"], data["finished"]) == (2, 1, 0)


async def test_snapshot_is_cached_briefly(board, session_factory):
    first = await board.snapshot()
    await register_users(session_factory, 1, 2, 3)
    assert (await board.snapshot())["registered"] == first["registered"] == 0  # у межах TTL: з кешу
    board.ticker.t += server.CACHE_TTL + 0.1
    assert (await board.snapshot())["registered"] == 3  # після TTL: свіжі дані


async def test_board_uses_current_content_after_reload(session_factory):
    holder = {"content": make_content()}
    board = BoardData(session_factory, lambda: holder["content"], clock=Ticker())
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    assert (await board.snapshot())["total_questions"] == 5
    holder["content"].rounds[0].sections[0].questions.pop()
    board._cached = None
    assert (await board.snapshot())["total_questions"] == 4


# --- команда /board ------------------------------------------------------------


async def test_board_command_gives_link_to_admin_only(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h = Harness(session_factory, board_url="https://board.example.com", board_token="abc123")
    await h.send(ADMIN_ID, "/board")
    assert "https://board.example.com/board?token=abc123" in h.last_text(ADMIN_ID)
    assert "не публікуй" in h.last_text(ADMIN_ID)

    await h.send(555, "/board")
    assert h.sent(555) == []


async def test_board_command_when_disabled(session_factory):
    async with session_factory.begin() as s:
        await repo.ensure_game_state(s)
    h = Harness(session_factory, board_token="")
    await h.send(ADMIN_ID, "/board")
    assert "вимкнено" in h.last_text(ADMIN_ID)


def test_board_url_setting_defaults_and_trims():
    from bot.config import Settings

    assert Settings(_env_file=None, web_port=9000).board_url == "http://localhost:9000"
    assert Settings(_env_file=None, board_public_url="https://b.example.com/").board_url == "https://b.example.com"


async def test_real_tcp_server_start_and_cleanup(board):
    import socket

    import aiohttp

    with socket.socket() as sock:  # вільний порт
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    runner = await server.start_board(create_app(board, TOKEN), port)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/api/board?token={TOKEN}") as resp:
                assert resp.status == 200 and (await resp.json())["phase"] == "DRAFT"
            async with http.get(f"http://127.0.0.1:{port}/api/board") as resp:
                assert resp.status == 403
    finally:
        await runner.cleanup()
