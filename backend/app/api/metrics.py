"""Prometheus scrape endpoint (M19, Phase 7).

Outside `/api/v1` and outside the permission system, because a scraper has no identity
to present. What keeps it private is the network: nginx answers `/metrics` with a 404
instead of proxying it, so it is reachable only from inside the `ai-platform` Docker
network — which is where Prometheus runs. That is the arrangement docs/api.md records,
and it is the reason this router is registered separately in `app.main`.

Nothing here is expensive by accident. Counters and histograms are already in memory;
the only work a scrape does is one round of small aggregate queries for the fleet
gauges, guarded by a timeout so a slow database degrades the metrics rather than the
platform.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response

from app.api.deps import SessionDep
from app.core import metrics
from app.core.logging import get_logger
from app.services.monitoring import collect_fleet_state

log = get_logger(__name__)

router = APIRouter(tags=["monitoring"])

#: A scrape that hangs is worse than a scrape that misses gauges: Prometheus blocks its
#: scrape slot, the target goes stale, and the alert that fires says "target down" about
#: a platform that is serving traffic perfectly well.
_COLLECT_TIMEOUT_SECONDS = 5.0


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    include_in_schema=False,  # not part of the platform API surface
    response_class=Response,
)
async def prometheus_metrics(session: SessionDep) -> Response:
    try:
        async with asyncio.timeout(_COLLECT_TIMEOUT_SECONDS):
            await collect_fleet_state(session)
    except TimeoutError:
        # Serve what is in memory. The counters — requests, tokens, runs — are the ones
        # alerts are built on, and they are unaffected by the database being slow.
        log.warning("metrics_fleet_collection_timeout", timeout=_COLLECT_TIMEOUT_SECONDS)
    except Exception:  # pragma: no cover — defensive
        log.warning("metrics_fleet_collection_failed", exc_info=True)

    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)
