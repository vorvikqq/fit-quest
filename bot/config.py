"""Налаштування з `.env` (DESIGN §14)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = ""
    # "111,222" у .env → [111, 222]
    admin_ids: Annotated[list[int], NoDecode] = []
    database_url: str = "postgresql+asyncpg://quest:quest@db:5432/quest"
    access_code: str = ""  # початкове значення, далі /setcode
    board_token: str = ""
    web_port: int = 8080
    tz: str = "Europe/Kyiv"
    log_level: str = "INFO"
    content_path: Path = PROJECT_ROOT / "content" / "quest.yaml"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    @property
    def content_root(self) -> Path:
        """Каталог, від якого рахуються шляхи `image` у quest.yaml."""
        return self.content_path.parent


def get_settings() -> Settings:
    return Settings()
