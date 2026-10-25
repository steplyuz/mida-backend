"""Async engine and session factory. SQLite foreign keys are enforced on every connection."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, pool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _enable_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


def create_engine_from_url(
    url: str, *, poolclass: type[pool.Pool] | None = None, echo: bool = False
) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
    if poolclass is not None:
        kwargs["poolclass"] = poolclass
    engine = create_async_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        _enable_sqlite_pragmas(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
