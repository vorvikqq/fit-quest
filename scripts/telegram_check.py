"""Діагностика каналу до Telegram: затримка API і швидкість завантаження фото.

Показує, чи проблема «довго вантажить» у мережі сервера/комп'ютера, а не в боті. Нічого не змінює:
надсилає одну тестову картинку першому адміну й одразу видаляє її.

З контейнера (там, де працює бот):
    docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts:ro" bot python scripts/telegram_check.py

Орієнтири: getMe < 0,5 с; завантаження 200 КБ < 3 с. Якщо десятки секунд або таймаут,
канал до Telegram слабкий (VPN, провайдер, Wi-Fi), а не код.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402
from aiogram.exceptions import TelegramAPIError  # noqa: E402
from aiogram.types import FSInputFile  # noqa: E402

from bot.config import get_settings  # noqa: E402

PINGS = 5
UPLOAD_TIMEOUT = 90.0


def verdict(seconds: float, good: float, ok: float) -> str:
    return "добре" if seconds <= good else "повільно" if seconds <= ok else "ДУЖЕ ПОВІЛЬНО"


async def main() -> int:
    settings = get_settings()
    if not settings.bot_token:
        print("BOT_TOKEN порожній")
        return 1
    bot = Bot(settings.bot_token, session=AiohttpSession(timeout=UPLOAD_TIMEOUT))
    try:
        timings = []
        for _ in range(PINGS):
            started = time.perf_counter()
            await bot.get_me()
            timings.append(time.perf_counter() - started)
        median = statistics.median(timings)
        print(f"getMe x{PINGS}: медіана {median * 1000:.0f} мс, макс {max(timings) * 1000:.0f} мс "
              f"({verdict(median, 0.5, 1.5)})")  # fmt: skip

        images = sorted((settings.content_root / "images").glob("*.jpg")) or sorted(
            (settings.content_root / "images").glob("*.png")
        )
        if not images or not settings.admin_ids:
            print("Немає картинок або ADMIN_IDS: перевірку завантаження пропущено")
            return 0
        image = max(images, key=lambda p: p.stat().st_size)
        size_kb = image.stat().st_size / 1024
        started = time.perf_counter()
        msg = await bot.send_photo(settings.admin_ids[0], FSInputFile(image), caption="тест каналу")
        elapsed = time.perf_counter() - started
        print(f"завантаження {image.name} ({size_kb:.0f} КБ): {elapsed:.1f} с, "
              f"≈{size_kb / elapsed:.0f} КБ/с ({verdict(elapsed, 3, 10)})")  # fmt: skip
        await bot.delete_message(settings.admin_ids[0], msg.message_id)
        return 0
    except TelegramAPIError as exc:
        print(f"ПОМИЛКА: {exc}\nКанал до Telegram нестабільний або заблокований.")
        return 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main()))
