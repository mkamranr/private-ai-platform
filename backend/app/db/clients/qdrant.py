"""Qdrant client (M01).

Deliberately *not* a :class:`~app.core.interfaces.vector.VectorStore`
implementation. This wrapper exists only so ``/health/ready`` can report on Qdrant
from Phase 0; ``QdrantVectorStore`` arrives in Phase 5 and is what application
code will use. Keeping them separate stops the health check from becoming an
accidental second access path to the vector database.
"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient

from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class QdrantClientWrapper:
    def __init__(self, settings: Settings) -> None:
        api_key = settings.qdrant.api_key
        self._client = AsyncQdrantClient(
            host=settings.qdrant.host,
            port=settings.qdrant.port,
            grpc_port=settings.qdrant.grpc_port,
            prefer_grpc=settings.qdrant.prefer_grpc,
            api_key=api_key.get_secret_value() if api_key else None,
            timeout=int(settings.qdrant.timeout_seconds),
        )

    @property
    def client(self) -> AsyncQdrantClient:
        return self._client

    async def ping(self) -> None:
        """Raise if Qdrant is unreachable.

        ``get_collections`` is the cheapest call that proves the service is
        actually serving requests, not merely accepting TCP connections.
        """
        await self._client.get_collections()

    async def close(self) -> None:
        await self._client.close()
        log.info("qdrant_client_closed")
