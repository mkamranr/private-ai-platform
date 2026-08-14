"""Clients for the platform's supporting data services (M01).

Each wrapper exposes ``ping()``, which raises on failure and returns ``None`` on
success. Timing, timeouts and aggregation live in
:class:`~app.services.health.HealthService`, so a client stays a thin adapter and
readiness policy stays in one place.

Startup performs no I/O against these services. A control plane that refuses to
boot because MinIO is briefly down would violate §25's requirement that the
platform survive individual container restarts — so connection problems surface
through ``/health/ready`` instead, where an operator can actually see them.
"""

from app.db.clients.minio import MinioClient
from app.db.clients.qdrant import QdrantClientWrapper
from app.db.clients.redis import RedisClient

__all__ = ["MinioClient", "QdrantClientWrapper", "RedisClient"]
