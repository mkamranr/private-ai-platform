"""Async engine and session management (M01)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class Database:
    """Owns the engine and session factory for the process lifetime.

    Created once in the application lifespan and disposed on shutdown. An engine
    per request would exhaust Postgres connections under any real load.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.database.dsn,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            # Validates a pooled connection before handing it out. Costs one
            # round-trip but survives a Postgres restart, which §25 requires:
            # "the platform should survive individual container restarts".
            pool_pre_ping=settings.database.pool_pre_ping,
            echo=settings.database.echo,
            connect_args={"timeout": settings.database.connect_timeout_seconds},
        )
        self._sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    async def dispose(self) -> None:
        await self._engine.dispose()
        log.info("database_engine_disposed")


async def session_scope(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, committing on success and rolling back on failure.

    One transaction per request. A handler that raises leaves nothing
    half-written, which matters most for audit records: a partially applied
    privileged action with no audit row is exactly the state §M24 exists to
    prevent.
    """
    session = sessionmaker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
