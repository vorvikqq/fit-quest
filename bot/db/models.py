"""SQLAlchemy-таблиці (DESIGN §4).

Питання в БД не зберігаються: `answers.qid` посилається на `id` з quest.yaml.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Phase(enum.StrEnum):
    DRAFT = "DRAFT"
    REGISTRATION = "REGISTRATION"
    RUNNING = "RUNNING"
    CLOSED = "CLOSED"
    RESULTS = "RESULTS"


class AttemptStatus(enum.StrEnum):
    ROUND_INTRO = "ROUND_INTRO"
    QUESTION = "QUESTION"
    FINISHED = "FINISHED"
    CLOSED_UNFINISHED = "CLOSED_UNFINISHED"


class UserStatus(enum.StrEnum):
    FIT_STUDENT = "FIT_STUDENT"
    APPLICANT = "APPLICANT"
    OTHER_STUDENT = "OTHER_STUDENT"
    OTHER = "OTHER"


COURSES = ("1", "2", "3", "4", "MASTER")


def _in(column: str, values: Any) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class Base(DeclarativeBase):
    pass


def _tstz() -> Any:
    return DateTime(timezone=True)


class GameState(Base):
    """Рівно один рядок, id = 1."""

    __tablename__ = "game_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="game_state_single_row"),
        CheckConstraint(_in("phase", [p.value for p in Phase]), name="game_state_phase"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    phase: Mapped[str] = mapped_column(Text, default=Phase.DRAFT.value, server_default=Phase.DRAFT.value)
    registration_open: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    access_code: Mapped[str] = mapped_column(Text, default="", server_default="")  # case-insensitive, trim
    started_at: Mapped[datetime | None] = mapped_column(_tstz())
    closes_at: Mapped[datetime | None] = mapped_column(_tstz())  # плановий час закриття
    closed_at: Mapped[datetime | None] = mapped_column(_tstz())
    content_hash: Mapped[str | None] = mapped_column(Text)  # хеш quest.yaml на момент старту


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IS NULL OR " + _in("status", [s.value for s in UserStatus]), name="users_status"
        ),
        CheckConstraint("course IS NULL OR " + _in("course", COURSES), name="users_course"),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(Text)
    tg_name: Mapped[str] = mapped_column(Text, default="", server_default="")  # first+last з Telegram
    full_name: Mapped[str | None] = mapped_column(Text)  # введене при реєстрації
    status: Mapped[str | None] = mapped_column(Text)
    course: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime | None] = mapped_column(_tstz())
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())  # FR-16
    blocked_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        CheckConstraint(_in("status", [s.value for s in AttemptStatus]), name="attempts_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # UNIQUE: одна спроба на акаунт (FR-6), захист від подвійного старту
    tg_id: Mapped[int] = mapped_column(ForeignKey("users.tg_id"), unique=True)
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(_tstz())
    finished_at: Mapped[datetime | None] = mapped_column(_tstz())
    # персональний порядок: [{"qid": ..., "option_order": [2, 0, 3, 1]}, ...]
    plan: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    cursor: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # індекс у plan
    q_sent_at: Mapped[datetime | None] = mapped_column(_tstz())
    q_deadline: Mapped[datetime | None] = mapped_column(_tstz())
    q_message_id: Mapped[int | None] = mapped_column(BigInteger)
    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_time_ms: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    promo_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    results_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    feedback: Mapped[str | None] = mapped_column(Text)


class Answer(Base):
    __tablename__ = "answers"

    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    # PK (attempt_id, qid) — захист від подвійного зарахування (NFR-4)
    qid: Mapped[str] = mapped_column(Text, primary_key=True)
    # індекс в ОРИГІНАЛЬНОМУ порядку з yaml; NULL = таймаут
    chosen: Mapped[int | None] = mapped_column(Integer)
    is_correct: Mapped[bool] = mapped_column(Boolean)
    response_ms: Mapped[int] = mapped_column(Integer)
    answered_at: Mapped[datetime] = mapped_column(_tstz())


class MediaCache(Base):
    __tablename__ = "media_cache"

    path: Mapped[str] = mapped_column(Text, primary_key=True)  # content/images/xxx.png
    sha256: Mapped[str] = mapped_column(Text)  # змінився файл → перезавантажити
    file_id: Mapped[str] = mapped_column(Text)
