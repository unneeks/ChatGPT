"""Async engine/session factory. One small pool — B1ms Postgres caps connections (~35)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from reqsmith.settings import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {}
        if settings.database_url.startswith("postgresql"):
            kwargs = {"pool_size": 5, "max_overflow": 2}
        _engine = create_async_engine(settings.database_url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        async with session.begin():
            yield session


def reset_db_state() -> None:
    """Test hook: drop cached engine/factory (e.g. when DATABASE_URL changes)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
