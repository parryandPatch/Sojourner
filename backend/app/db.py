"""Database session management and schema creation."""

from __future__ import annotations

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine, select

from app.config import get_settings
from app.domain import models as _models  # noqa: F401  (register tables)

_settings = get_settings()

_connect_args: dict = {}
if _settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    _settings.database_url,
    echo=_settings.sql_echo,
    connect_args=_connect_args,
)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    with Session(engine) as session:
        yield session


def session_scope() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


__all__ = ["engine", "get_session", "init_db", "select", "session_scope"]