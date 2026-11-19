"""Кеш `file_id` картинок (DESIGN §8, NFR-6).

Під час гри картинки йдуть лише через `file_id`. Якщо файл змінився (інший sha256) або
`file_id` ще нема, бот завантажує файл і кешує `file_id` на льоту.
"""

from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.content.models import Content
from bot.db import repo


@dataclass
class WarmupResult:
    uploaded: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


class MediaService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], content_root: Path) -> None:
        self._session_factory = session_factory
        self.content_root = content_root
        self._sha_cache: dict[str, tuple[float, str]] = {}  # rel → (mtime, sha256)

    def path(self, rel: str) -> Path:
        return self.content_root / rel

    def sha256(self, rel: str) -> str:
        """Хеш файлу; перераховується лише коли змінився mtime."""
        path = self.path(rel)
        mtime = path.stat().st_mtime
        cached = self._sha_cache.get(rel)
        if cached and cached[0] == mtime:
            return cached[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._sha_cache[rel] = (mtime, digest)
        return digest

    async def cached_file_id(self, rel: str) -> str | None:
        """`file_id`, якщо файл не змінився з моменту кешування."""
        async with self._session_factory() as session:
            row = await repo.get_media(session, rel)
        if row is not None and row.sha256 == self.sha256(rel):
            return row.file_id
        return None

    async def remember(self, rel: str, file_id: str) -> None:
        async with self._session_factory.begin() as session:
            await repo.set_media(session, rel, self.sha256(rel), file_id)

    @staticmethod
    def images_of(content: Content) -> list[str]:
        return sorted({q.image for _, _, q in content.iter_questions() if q.image})

    async def cache_status(self, content: Content) -> tuple[int, int]:
        """(скільки картинок уже має актуальний file_id, скільки всього) для панелі адміна."""
        images = self.images_of(content)
        cached = 0
        for rel in images:
            if self.path(rel).is_file() and await self.cached_file_id(rel) is not None:
                cached += 1
        return cached, len(images)

    async def warmup(
        self, bot: Bot, chat_id: int, content: Content, *, cleanup: bool = False
    ) -> WarmupResult:
        """`/warmup`: надсилає адміну картинки без актуального `file_id` й запам'ятовує його.

        `cleanup=True` видаляє службові повідомлення з чату адміна одразу після кешування
        (так робить автоматичний прогрів при `/start_quest`, щоб не засмічувати чат).
        """
        result = WarmupResult()
        for rel in self.images_of(content):
            if not self.path(rel).is_file():
                result.missing.append(rel)
            elif await self.cached_file_id(rel) is not None:
                result.cached.append(rel)
            else:
                msg = await bot.send_photo(chat_id, FSInputFile(self.path(rel)), caption=rel)
                await self.remember(rel, msg.photo[-1].file_id)
                result.uploaded.append(rel)
                if cleanup:
                    with contextlib.suppress(TelegramAPIError):
                        await bot.delete_message(chat_id, msg.message_id)
        return result
