from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from .config import settings


engine = create_engine(
    settings.database_url,
    pool_size=3,
    max_overflow=2,
    pool_pre_ping=True,
    pool_recycle=240,
)


@contextmanager
def transaction() -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


def one(connection: Connection, statement: str, values: dict[str, Any] | None = None) -> dict[str, Any] | None:
    row = connection.execute(text(statement), values or {}).mappings().fetchone()
    return dict(row) if row is not None else None


def all_rows(connection: Connection, statement: str, values: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(statement), values or {}).mappings().fetchall()]


def execute(connection: Connection, statement: str, values: dict[str, Any] | None = None) -> None:
    connection.execute(text(statement), values or {})

