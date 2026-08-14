"""Redis/Valkey client (M01).

Valkey is the default deployment (see docker-compose.yml); the spec permits either
and ``redis-py`` speaks to both unchanged.

Used for session state and short-term agent memory (§M16), and for the gateway's
rate-limit counters (M09).
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis, from_url

from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class RedisClient:
    """Thin wrapper owning one connection pool."""

    def __init__(self, settings: Settings) -> None:
        self._client: Redis = from_url(
            settings.redis.dsn,
            socket_timeout=settings.redis.socket_timeout_seconds,
            socket_connect_timeout=settings.redis.socket_timeout_seconds,
            decode_responses=True,
            health_check_interval=30,
        )

    @property
    def client(self) -> Redis:
        return self._client

    async def ping(self) -> None:
        """Raise if the server is unreachable."""
        await self._client.ping()

    async def info(self) -> dict[str, Any]:
        return await self._client.info(section="server")

    async def close(self) -> None:
        await self._client.aclose()
        log.info("redis_client_closed")
