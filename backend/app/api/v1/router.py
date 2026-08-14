"""v1 API router aggregation (M01).

Each module registers its router here as it lands. Keeping assembly in one file
means the full API surface is readable at a glance, which matters when §8 defines
roughly a hundred endpoints across 28 modules.

All 28 modules' routers are registered here.
"""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    audit,
    auth,
    dashboard,
    health,
    infrastructure,
    knowledge,
    models,
    monitoring,
    users,
    voice,
)

api_router = APIRouter()

# Health first — it must resolve even if a later router fails to import.
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
# Phase 1: nodes, GPUs, containers, allocations.
api_router.include_router(infrastructure.router)
# Phase 2: model registry, deployments, aliases, API keys.
api_router.include_router(models.router)
# Phase 3: the operator landing page.
api_router.include_router(dashboard.router)
# Phase 4: agents, skills, tools, MCP servers and runs.
api_router.include_router(agents.router)
# Phase 5: knowledge bases, documents and memory.
api_router.include_router(knowledge.router)
# Phase 6: the audit log, read-only.
api_router.include_router(audit.router)
# Phase 7: the observability overview and one trace at a time. The Prometheus scrape
# endpoint is NOT here — it carries no permission and must not sit under the API prefix
# nginx proxies. See app.api.metrics.
api_router.include_router(monitoring.router)
# M29: voice sessions and the assistant's configuration. The conversation itself runs on
# the WebSocket in app.api.voice_ws, which is not a REST resource and is mounted in
# app.main alongside /metrics.
api_router.include_router(voice.router)

# The OpenAI-compatible gateway is deliberately NOT here. It mounts at `/v1` in
# app.main, because the SDK derives its paths from base_url and `GET {base}/models`
# would otherwise collide with the registry above — same path, different resource,
# different credential. See app/api/v1/gateway.py.
