"""Кеш `file_id` картинок (DESIGN §8, NFR-6).

Під час гри картинки йдуть лише через `file_id`. Якщо файл змінився (інший sha256) або
`file_id` ще нема, бот завантажує файл і кешує `file_id` на льоту.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db import repo


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
