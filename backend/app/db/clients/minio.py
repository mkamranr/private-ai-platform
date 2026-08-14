"""MinIO object storage client (M01).

The ``minio`` SDK is synchronous, so every call is offloaded with
``asyncio.to_thread``. Calling it directly from a coroutine would block the event
loop for the duration of an object transfer — which for a model file or a large
PDF is long enough to stall every other request in the process.

Stores uploaded documents (M15), model artefacts and backup archives (M25).
"""

from __future__ import annotations

import asyncio

from minio import Minio

from app.config.settings import Settings
from app.core.logging import get_logger

log = get_logger(__name__)


class MinioClient:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.minio.bucket
        self._client = Minio(
            endpoint=settings.minio.endpoint,
            access_key=settings.minio.access_key,
            secret_key=settings.minio.secret_key.get_secret_value(),
            secure=settings.minio.secure,
            region=settings.minio.region,
        )

    @property
    def client(self) -> Minio:
        return self._client

    @property
    def bucket(self) -> str:
        return self._bucket

    async def ping(self) -> None:
        """Raise if MinIO is unreachable.

        ``bucket_exists`` proves credentials work as well as reachability; a bare
        TCP check would pass with a wrong secret key and let a misconfiguration
        reach production looking healthy.
        """
        await asyncio.to_thread(self._client.bucket_exists, self._bucket)

    async def ensure_bucket(self) -> bool:
        """Create the platform bucket if absent. Returns True when it was created.

        Called from ``make seed``, not from startup — bootstrap side effects belong
        in an explicit, re-runnable step.
        """
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if exists:
            return False
        await asyncio.to_thread(self._client.make_bucket, self._bucket)
        log.info("minio_bucket_created", bucket=self._bucket)
        return True

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return the key.

        The MinIO SDK is synchronous, so the call goes to a thread: a 50 MB upload written
        on the event loop would stall every other request for its duration.
        """
        import asyncio
        import io

        await asyncio.to_thread(
            self._client.put_object,
            self.bucket,
            key,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return key

    async def get_object(self, key: str) -> bytes:
        """Read an object back. Raises if it is gone."""
        import asyncio

        def read() -> bytes:
            response = self._client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                # Both required by the SDK; skipping them leaks the connection back into
                # the pool in an unusable state.
                response.close()
                response.release_conn()

        return await asyncio.to_thread(read)

    async def remove_object(self, key: str) -> None:
        import asyncio

        await asyncio.to_thread(self._client.remove_object, self.bucket, key)

    async def close(self) -> None:
        # The SDK holds a urllib3 pool with no public close; drop the reference.
        log.info("minio_client_closed")
