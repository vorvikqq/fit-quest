from __future__ import annotations

import importlib.util
import shutil
import sys

import pytest

from bot.config import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location("preflight", PROJECT_ROOT / "scripts" / "preflight.py")
preflight = importlib.util.module_from_spec(_spec)
sys.modules["preflight"] = preflight
_spec.loader.exec_module(preflight)

GOOD = """
BOT_TOKEN=123456789:AAH_abcdefghijklmnopqrstuvwxyz012345
ADMIN_IDS=111,222
ACCESS_CODE=Hello
BOARD_TOKEN={token}
BOARD_PUBLIC_URL=https://board.example.com
"""
TOKEN = "a" * 48


def env_file(tmp_path, text=GOOD, **fmt):
    path = tmp_path / ".env"
    path.write_text(text.format(token=fmt.get("token", TOKEN)), encoding="utf-8")
    return path


def run(path):
    return preflight.run(path, skip_docker=True)


def test_good_setup_has_no_errors_or_warnings(tmp_path):
    result = run(env_file(tmp_path))
    assert result.errors == [] and result.warnings == []
    assert any("50 питань" in line for line in result.ok)


def test_missing_env_file(tmp_path):
    result = run(tmp_path / "nope.env")
    assert any("немає файлу" in e for e in result.errors)


@pytest.mark.parametrize("token", ["", "abc", "123:short", "not-a-token"])
def test_bad_bot_token_is_error(tmp_path, token):
    path = env_file(tmp_path, GOOD.replace("123456789:AAH_abcdefghijklmnopqrstuvwxyz012345", token))
    assert any("BOT_TOKEN" in e for e in run(path).errors)


def test_empty_admin_ids_is_error(tmp_path):
    path = env_file(tmp_path, GOOD.replace("ADMIN_IDS=111,222", "ADMIN_IDS="))
    assert any("ADMIN_IDS" in e for e in run(path).errors)


def test_garbage_admin_ids_is_reported_not_crash(tmp_path):
    path = env_file(tmp_path, GOOD.replace("111,222", "abc"))
    assert any("не читається" in e for e in run(path).errors)


def test_warnings_for_soft_problems(tmp_path):
    text = GOOD.replace("ACCESS_CODE=Hello", "ACCESS_CODE=").replace("BOARD_PUBLIC_URL=https://board.example.com", "BOARD_PUBLIC_URL=")
    result = run(env_file(tmp_path, text, token="short"))
    joined = " ".join(result.warnings)
    assert "ACCESS_CODE" in joined and "BOARD_TOKEN занадто короткий" in joined and "localhost" in joined
    assert result.errors == []


def test_empty_board_token_only_warns(tmp_path):
    result = run(env_file(tmp_path, token=""))
    assert result.errors == [] and any("табло буде вимкнене" in w for w in result.warnings)


def test_plain_http_board_gets_warning(tmp_path):
    text = GOOD.replace("https://board.example.com", "http://203.0.113.5:8080")
    assert any("HTTP" in w for w in run(env_file(tmp_path, text)).warnings)


def test_content_with_todo_blocks_deploy(tmp_path):
    content = tmp_path / "quest.yaml"
    shutil.copy(PROJECT_ROOT / "content" / "quest.yaml", content)
    content.write_text(content.read_text(encoding="utf-8").replace("correct: 1\n", "correct: null\n", 1), encoding="utf-8")
    path = env_file(tmp_path, GOOD + f"CONTENT_PATH={content}\n")
    result = run(path)
    assert any("незаповнених" in e for e in result.errors)


def test_content_error_blocks_deploy(tmp_path):
    content = tmp_path / "quest.yaml"
    content.write_text("rounds: [", encoding="utf-8")
    result = run(env_file(tmp_path, GOOD + f"CONTENT_PATH={content}\n"))
    assert any("контент не читається" in e for e in result.errors)


def test_cli_exit_codes(tmp_path, capsys):
    assert preflight.main(["--env", str(env_file(tmp_path)), "--skip-docker"]) == 0
    assert "Готово до деплою" in capsys.readouterr().out
    assert preflight.main(["--env", str(tmp_path / "nope"), "--skip-docker"]) == 1
    assert "Деплоїти не можна" in capsys.readouterr().out
