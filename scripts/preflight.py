"""Перевірка перед деплоєм: `.env`, контент, Docker. Нічого не змінює й не запускає.

    python scripts/preflight.py            # перевіряє ./.env
    python scripts/preflight.py --env /шлях/до/.env

Код виходу: 1, якщо є ПОМИЛКИ (деплоїти не можна); 0, якщо є лише попередження чи все гаразд.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import Settings  # noqa: E402
from bot.content.loader import ContentError, load_and_validate  # noqa: E402

TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")
MIN_BOARD_TOKEN = 24


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: list[str] = field(default_factory=list)


def check_env(env_path: Path, result: Result) -> Settings | None:
    if not env_path.is_file():
        result.errors.append(f"немає файлу {env_path}: `cp .env.example .env` і заповни")
        return None
    try:
        settings = Settings(_env_file=env_path)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"{env_path.name} не читається: {exc}")
        return None

    if not TOKEN_RE.match(settings.bot_token):
        result.errors.append("BOT_TOKEN порожній або має неправильний вигляд (очікується 123456:ABC…)")
    else:
        result.ok.append("BOT_TOKEN має правильний формат (дійсність перевіряє лише Telegram)")

    if not settings.admin_ids:
        result.errors.append("ADMIN_IDS порожній: адмін-команди будуть недоступні нікому")
    else:
        result.ok.append(f"ADMIN_IDS: {len(settings.admin_ids)} адмін(ів)")

    if not settings.board_token:
        result.warnings.append("BOARD_TOKEN порожній: живе табло буде вимкнене")
    elif len(settings.board_token) < MIN_BOARD_TOKEN:
        result.warnings.append(
            f"BOARD_TOKEN занадто короткий ({len(settings.board_token)}): `openssl rand -hex 24`"
        )
    else:
        result.ok.append("BOARD_TOKEN достатньо довгий")

    if not settings.access_code.strip():
        result.warnings.append("ACCESS_CODE порожній: не забудь задати кодове слово командою /setcode")
    if settings.board_token and not settings.board_public_url:
        result.warnings.append("BOARD_PUBLIC_URL порожній: команда /board дасть посилання на localhost")
    elif settings.board_public_url.startswith("http://") and "localhost" not in settings.board_public_url:
        result.warnings.append("табло по HTTP без шифрування: не публікуй посилання з токеном у чаті Zoom")
    return settings


def check_content(content_path: Path, result: Result) -> None:
    try:
        content, report = load_and_validate(content_path)
    except (ContentError, OSError) as exc:
        result.errors.append(f"контент не читається: {exc}")
        return
    for issue in report.errors:
        result.errors.append(f"контент: {issue.where}: {issue.message}")
    if report.has_todo:
        ids = ", ".join(sorted({i.where for i in report.todos}))
        result.errors.append(f"у контенті {len(report.todos)} незаповнених (TODO): {ids}")
    for issue in report.warnings:
        result.warnings.append(f"контент: {issue.where}: {issue.message}")
    if report.ok and not report.has_todo:
        result.ok.append(f"контент: {len(content.rounds)} раундів, {content.question_count} питань, без TODO")


def check_docker(result: Result) -> None:
    if shutil.which("docker") is None:
        result.warnings.append("docker не знайдено: перевірку compose пропущено")
        return
    proc = subprocess.run(
        ["docker", "compose", "config", "-q"], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    if proc.returncode == 0:
        result.ok.append("docker compose config: коректний")
    else:
        result.errors.append(f"docker compose config: {proc.stderr.strip()[:200]}")


def run(env_path: Path, skip_docker: bool = False) -> Result:
    result = Result()
    settings = check_env(env_path, result)
    check_content(settings.content_path if settings else ROOT / "content" / "quest.yaml", result)
    if not skip_docker:
        check_docker(result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--skip-docker", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    result = run(args.env, args.skip_docker)
    for line in result.ok:
        print(f"  ok       {line}")
    for line in result.warnings:
        print(f"  УВАГА    {line}")
    for line in result.errors:
        print(f"  ПОМИЛКА  {line}")
    if result.errors:
        print(f"\nДеплоїти не можна: помилок {len(result.errors)}.")
        return 1
    print(f"\nГотово до деплою (попереджень: {len(result.warnings)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
