"""Async database engine / session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(database_url: str, *, in_memory_shared: bool = False) -> AsyncEngine:
    kwargs: dict = {"echo": False, "future": True}
    if database_url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        if in_memory_shared or ":memory:" in database_url:
            # share one in-memory database across connections (tests)
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
    return create_async_engine(database_url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def reset_engine() -> None:
    """Dispose cached engine (used by tests to switch databases)."""
    global _engine, _sessionmaker
    if _engine is not None:
        pass  # disposal handled by caller holding a reference
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a database session."""
    async with get_sessionmaker()() as session:
        yield session
