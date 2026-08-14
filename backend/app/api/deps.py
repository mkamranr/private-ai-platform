"""FastAPI dependencies — the platform's authorisation seam (M03, Rule 6).

Routers depend on *services* and on :func:`require_permission`. They never see a
session or a repository; the import-linter contract in ``pyproject.toml`` enforces
that, because "just this once" is how a layered architecture stops being one.

:func:`require_permission` is the single authorisation primitive in the codebase.
Every mutating route declares one from Phase 0 onward. That ordering is the whole
reason auth was pulled ahead of the spec's Phase 6: adding the check as each route
is written costs a line, whereas retrofitting it across ~18 modules of existing
routes is where authorisation gaps get shipped.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.core.errors import PermissionDeniedError, RateLimitError, TokenError
from app.core.interfaces.auth_provider import AuthProvider, PasswordAuthProvider
from app.core.interfaces.tools import ToolType
from app.core.interfaces.vector import VectorStore
from app.core.logging import get_logger
from app.core.request_context import get_client_ip
from app.core.security import PasswordHasherService, SecretCipher, TokenService
from app.db.session import session_scope
from app.models.auth import User
from app.models.infrastructure import NodeEnrollment
from app.repositories.agents import (
    AgentRepository,
    AgentRunEventRepository,
    AgentRunRepository,
    AgentVersionRepository,
    McpServerRepository,
    SkillRepository,
    ToolExecutionRepository,
    ToolRepository,
)
from app.repositories.audit import AuditRepository
from app.repositories.infrastructure import (
    ContainerRepository,
    GpuAllocationRepository,
    GpuHealthEventRepository,
    GpuMetricRepository,
    GpuProcessRepository,
    GpuRepository,
    NodeEnrollmentRepository,
    NodeRepository,
)
from app.repositories.knowledge import (
    ConversationMessageRepository,
    ConversationRepository,
    DocumentChunkRepository,
    DocumentRepository,
    EmbeddingModelRepository,
    KnowledgeBaseRepository,
    MemoryEntryRepository,
)
from app.repositories.models_registry import (
    ApiClientRepository,
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository
from app.repositories.voice import (
    VoiceEventRepository,
    VoiceMessageRepository,
    VoiceSessionRepository,
)
from app.services.agent_registry import (
    AgentRegistryService,
    McpRegistryService,
    SkillRegistryService,
    ToolRegistryService,
)
from app.services.agent_runs import AgentRunService
from app.services.agent_runtime import PlatformAgentRuntime
from app.services.api_keys import ApiKeyService
from app.services.audit import AuditService
from app.services.auth import AuthService
from app.services.auth_providers import LdapAuthProvider, LocalAuthProvider
from app.services.dashboard import DashboardService
from app.services.definitions import DefinitionImporter
from app.services.deployment import DeploymentService
from app.services.docker_service import DockerService, build_runtime_factory
from app.services.federation import FederationService
from app.services.gateway import GatewayContext, GatewayService
from app.services.health import HealthService
from app.services.identity import resolve_forwarded_identity
from app.services.infrastructure import GpuService, NodeService
from app.services.knowledge import KnowledgeService
from app.services.media_provider import ResolvingOcrEngine
from app.services.memory import MemoryService
from app.services.model_registry import ModelRegistryService
from app.services.monitoring import MonitoringService, TraceService
from app.services.node_enrollment import NodeEnrollmentService
from app.services.retrieval import RetrievalService
from app.services.tool_pipeline import ToolPipeline
from app.services.user import UserService
from app.services.vector_store import QdrantVectorStore
from app.services.voice import VoiceSessionService
from app.services.voice_config import VoiceConfigStore
from app.workers.deployments import _compute_backend_factory

# auto_error=False so a missing header raises our own TokenError, keeping the
# response shape identical to every other platform error.
log = get_logger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Platform JWT access token")


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------
def get_app_settings() -> Settings:
    return get_settings()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One transaction per request, committed on success (see session_scope)."""
    database = request.app.state.database
    async for session in session_scope(database.sessionmaker):
        yield session


def get_health_service(request: Request) -> HealthService:
    """Built once at startup: it holds long-lived clients, not per-request state."""
    return request.app.state.health_service  # type: ignore[no-any-return]


def get_password_hasher(request: Request) -> PasswordHasherService:
    """Shared instance — argon2 parameters are configuration, not per-request."""
    return request.app.state.password_hasher  # type: ignore[no-any-return]


def get_token_service(request: Request) -> TokenService:
    return request.app.state.token_service  # type: ignore[no-any-return]


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
HasherDep = Annotated[PasswordHasherService, Depends(get_password_hasher)]
TokensDep = Annotated[TokenService, Depends(get_token_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def get_audit_service(request: Request, session: SessionDep) -> AuditService:
    """Audit service with both write paths available.

    The session factory enables ``record_independent``, which commits denials and
    failures outside the request transaction so a rollback cannot erase them.
    """
    return AuditService(
        AuditRepository(session),
        session_factory=request.app.state.database.sessionmaker,
    )


AuditDep = Annotated[AuditService, Depends(get_audit_service)]


def get_audit_repository(session: SessionDep) -> AuditRepository:
    """The repository, not the service — reading the audit log records nothing itself.

    Routing a read through AuditService would give it access to `record()`, and a query
    endpoint that can write to the log it is querying is a hole nobody needs.
    """
    return AuditRepository(session)


AuditRepositoryDep = Annotated[AuditRepository, Depends(get_audit_repository)]


def get_auth_providers(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    hasher: HasherDep,
) -> list[AuthProvider]:
    """Every configured provider, in the order sign-in tries them.

    Rebuilt per request because the local provider holds a session-scoped repository. The
    OIDC provider's caches would be lost with it, so it is built once at startup and
    stashed on app.state — see below.
    """
    providers: list[AuthProvider] = [LocalAuthProvider(UserRepository(session), hasher)]
    if settings.ldap.enabled:
        providers.append(LdapAuthProvider(settings.ldap))
    oidc = getattr(request.app.state, "oidc_provider", None)
    if oidc is not None:
        providers.append(oidc)
    return providers


AuthProvidersDep = Annotated[list[AuthProvider], Depends(get_auth_providers)]


def get_redis(request: Request) -> Any:
    """The shared Redis client from the lifespan.

    Typed loosely on purpose: the concrete client is `redis.asyncio.Redis`, and naming it
    here would put a vendor type in the dependency surface that every route imports.
    """
    return request.app.state.redis.client


RedisDep = Annotated[Any, Depends(get_redis)]


def get_federation_service(
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
) -> FederationService:
    return FederationService(
        UserRepository(session), RoleRepository(session), audit, settings.federation
    )


FederationServiceDep = Annotated[FederationService, Depends(get_federation_service)]


def get_auth_service(
    session: SessionDep,
    hasher: HasherDep,
    tokens: TokensDep,
    audit: AuditDep,
    providers: AuthProvidersDep,
    federation: FederationServiceDep,
) -> AuthService:
    return AuthService(
        UserRepository(session),
        hasher,
        tokens,
        audit,
        # Only password providers reach login(); a redirect provider has its own routes,
        # and handing one a password would be a category error the type system catches.
        password_providers=[
            p for p in providers if isinstance(p, PasswordAuthProvider) and p.name != "local"
        ],
        federation=federation,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_user_service(
    session: SessionDep,
    hasher: HasherDep,
    audit: AuditDep,
) -> UserService:
    return UserService(
        UserRepository(session),
        RoleRepository(session),
        PermissionRepository(session),
        hasher,
        audit,
    )


UserServiceDep = Annotated[UserService, Depends(get_user_service)]


# ---------------------------------------------------------------------------
# Infrastructure (M04-M06)
# ---------------------------------------------------------------------------
def get_secret_cipher(request: Request) -> SecretCipher:
    """Built once at startup, so a malformed key fails there rather than mid-request."""
    return request.app.state.secret_cipher  # type: ignore[no-any-return]


CipherDep = Annotated[SecretCipher, Depends(get_secret_cipher)]


def get_node_service(
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
    cipher: CipherDep,
) -> NodeService:
    """Build a NodeService bound to this request's transaction.

    Agent tokens live encrypted in the database rather than in a process cache, so a
    control-plane restart does not silently stop the fleet being polled.
    """
    return NodeService(
        settings,
        NodeRepository(session),
        GpuRepository(session),
        GpuMetricRepository(session),
        GpuProcessRepository(session),
        GpuHealthEventRepository(session),
        ContainerRepository(session),
        audit,
        cipher,
    )


NodeServiceDep = Annotated[NodeService, Depends(get_node_service)]


def get_node_enrollment_service(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
    node_service: NodeServiceDep,
) -> NodeEnrollmentService:
    """Build the enrolment service for this request.

    It gets the session factory as well as the session: a failed enrolment must still
    record its attempt, and the request transaction is about to be rolled back by the
    error it is raising.
    """
    return NodeEnrollmentService(
        settings,
        NodeEnrollmentRepository(session),
        NodeRepository(session),
        node_service,
        audit,
        session_factory=request.app.state.database.sessionmaker,
    )


NodeEnrollmentServiceDep = Annotated[NodeEnrollmentService, Depends(get_node_enrollment_service)]


@dataclass(frozen=True, slots=True)
class EnrollmentContext:
    enrollment: NodeEnrollment
    source_ip: str | None


async def get_enrollment_context(
    request: Request,
    service: NodeEnrollmentServiceDep,
    redis: RedisDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> EnrollmentContext:
    """Authenticate a node presenting a one-time enrolment token.

    **Deliberately not the platform JWT**, and it must reject one: the caller is a shell
    script on a host that has no user account and never will. The same
    `Authorization: Bearer` header carries it, in a header rather than a query parameter
    because the access log records the request path and a token there would be permanent.

    Rate-limited per source address before the token is even looked at, so a caller
    guessing tokens pays the limit rather than the database.
    """
    source_ip = get_client_ip()
    await _limit_enrollment_attempts(redis, source_ip, settings.enrollment)

    presented = credentials.credentials if credentials else ""
    if not presented:
        raise TokenError(
            "A node enrolment token is required. Send it as 'Authorization: Bearer <token>'."
        )
    enrollment = await service.resolve_token(presented)
    return EnrollmentContext(enrollment=enrollment, source_ip=source_ip)


async def _limit_enrollment_attempts(redis: Any, source_ip: str | None, config: Any) -> None:
    """Fixed-window counter per source address, in Redis.

    Fails **open** on a Redis error, matching the gateway's limiter — and safe to do so
    here only because the real backstop is elsewhere: `max_attempts_per_token` is counted
    in Postgres, so it survives a cache outage and cannot be evaded by changing source
    address. Do not "fix" this fail-open without moving that guarantee somewhere else.
    """
    if not source_ip:
        return
    window = int(time.time() // 60)
    bucket = f"enroll:ratelimit:{source_ip}:{window}"
    try:
        count = await redis.incr(bucket)
        if count == 1:
            await redis.expire(bucket, 120)
    except Exception as exc:
        log.warning("enrollment_rate_limit_unavailable", error=type(exc).__name__)
        return
    if count > config.rate_limit_per_minute_per_ip:
        raise RateLimitError(
            "Too many enrolment attempts from this address. Wait a minute and retry."
        )


EnrollmentContextDep = Annotated[EnrollmentContext, Depends(get_enrollment_context)]


def get_gpu_service(session: SessionDep, settings: SettingsDep) -> GpuService:
    return GpuService(
        settings,
        GpuRepository(session),
        GpuMetricRepository(session),
        GpuProcessRepository(session),
        GpuHealthEventRepository(session),
        GpuAllocationRepository(session),
    )


GpuServiceDep = Annotated[GpuService, Depends(get_gpu_service)]


def get_docker_service(
    session: SessionDep,
    audit: AuditDep,
    node_service: NodeServiceDep,
) -> DockerService:
    """The Rule 7 chokepoint.

    Note it receives a *factory* rather than a runtime: which node a container lives on
    is only known per call, and the indirection is what keeps Kubernetes (§23) an
    addition rather than a rewrite.
    """
    return DockerService(
        NodeRepository(session),
        ContainerRepository(session),
        audit,
        build_runtime_factory(node_service),
    )


DockerServiceDep = Annotated[DockerService, Depends(get_docker_service)]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
async def get_current_user(
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """Resolve the bearer token to a live user record.

    The user is loaded from the database on every request rather than trusted from
    token claims, so disabling an account or revoking a role takes effect
    immediately instead of whenever the token happens to expire.
    """
    if credentials is None or not credentials.credentials:
        raise TokenError("An Authorization: Bearer <token> header is required.")
    return await auth.resolve_access_token(credentials.credentials)


CurrentUserDep = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User | None:
    """Resolve a user when a token is present, else ``None``.

    For endpoints that behave differently when authenticated but do not require it.
    """
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await auth.resolve_access_token(credentials.credentials)
    except TokenError:
        return None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
def require_permission(permission: str) -> Any:
    """Build a dependency asserting the caller holds ``permission``.

    Usage::

        @router.post("/models/{model_id}/deploy")
        async def deploy(
            user: Annotated[User, Depends(require_permission(Permission.MODEL_DEPLOY))],
        ) -> DeploymentRead: ...

    Returns the user, so a route needing both authorisation and identity declares
    one dependency rather than two. Refusals are audited by
    :meth:`AuthService.require_permission` before the 403 is raised.
    """

    async def dependency(user: CurrentUserDep, auth: AuthServiceDep) -> User:
        await auth.require_permission(user, permission)
        return user

    return Depends(dependency)


def require_superuser() -> Any:
    """Restrict to superusers, for operations no role should grant."""

    async def dependency(user: CurrentUserDep, audit: AuditDep) -> User:
        if not user.is_superuser:
            await audit.record_denied(
                "SUPERUSER_CHECK_FAILED",
                user_id=user.id,
                username=user.username,
                message="Superuser privileges required",
            )
            raise PermissionDeniedError("This action requires superuser privileges.")
        return user

    return Depends(dependency)


# ---------------------------------------------------------------------------
# Models, deployments and the gateway (M07-M09)
# ---------------------------------------------------------------------------
def get_model_registry(
    session: SessionDep, settings: SettingsDep, audit: AuditDep
) -> ModelRegistryService:
    return ModelRegistryService(
        settings,
        ModelRepository(session),
        ModelDeploymentRepository(session),
        ModelAliasRepository(session),
        audit,
    )


ModelRegistryDep = Annotated[ModelRegistryService, Depends(get_model_registry)]


def get_deployment_service(
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
    node_service: NodeServiceDep,
    gpu_service: GpuServiceDep,
) -> DeploymentService:
    """The deployment service, wired to a node-agent-backed compute backend.

    Shares the request's transaction, so the GPU reservation taken during
    `request_deployment` commits with the deployment row — or rolls back with it (§9).
    """
    return DeploymentService(
        settings,
        ModelRepository(session),
        ModelDeploymentRepository(session),
        NodeRepository(session),
        gpu_service,
        audit,
        _compute_backend_factory(settings, node_service),
    )


DeploymentServiceDep = Annotated[DeploymentService, Depends(get_deployment_service)]


# ---------------------------------------------------------------------------
# Agents (M10-M14)
# ---------------------------------------------------------------------------
def get_tool_executors(request: Request) -> dict[ToolType, Any]:
    """The executor table, built once at startup.

    Once, not per request: an executor holds no request state, and rebuilding the table
    per call would be pure waste on the agent hot path.
    """
    return request.app.state.tool_executors  # type: ignore[no-any-return]


def get_tool_pipeline(
    session: SessionDep, settings: SettingsDep, audit: AuditDep, request: Request
) -> ToolPipeline:
    """The §10 pipeline. Every tool call goes through this and nothing else."""
    return ToolPipeline(
        settings,
        ToolExecutionRepository(session),
        audit,
        get_tool_executors(request),
    )


ToolPipelineDep = Annotated[ToolPipeline, Depends(get_tool_pipeline)]


def get_tool_registry(
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
    cipher: CipherDep,
    request: Request,
) -> ToolRegistryService:
    return ToolRegistryService(
        settings, ToolRepository(session), audit, cipher, get_tool_executors(request)
    )


ToolRegistryDep = Annotated[ToolRegistryService, Depends(get_tool_registry)]


def get_mcp_registry(
    session: SessionDep, settings: SettingsDep, audit: AuditDep, cipher: CipherDep
) -> McpRegistryService:
    return McpRegistryService(
        settings, McpServerRepository(session), ToolRepository(session), audit, cipher
    )


McpRegistryDep = Annotated[McpRegistryService, Depends(get_mcp_registry)]


def get_skill_registry(session: SessionDep) -> SkillRegistryService:
    return SkillRegistryService(SkillRepository(session))


SkillRegistryDep = Annotated[SkillRegistryService, Depends(get_skill_registry)]


def get_agent_registry(
    session: SessionDep, settings: SettingsDep, audit: AuditDep
) -> AgentRegistryService:
    return AgentRegistryService(
        settings,
        AgentRepository(session),
        AgentVersionRepository(session),
        ToolRepository(session),
        SkillRepository(session),
        audit,
    )


AgentRegistryDep = Annotated[AgentRegistryService, Depends(get_agent_registry)]


def get_agent_runtime(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    pipeline: ToolPipelineDep,
    gateway: GatewayServiceDep,
    retrieval: RetrievalServiceDep,
) -> PlatformAgentRuntime:
    """The default runtime (see app/services/agent_runtime.py for why it is not LangGraph).

    Its provider factory goes through the **gateway**, so an agent reaches a model by
    exactly the same route a developer's API call does — same alias resolution, same
    deployment, same 503 when nothing is serving. A second path would eventually disagree
    with the first about what is deployed.
    """
    return PlatformAgentRuntime(
        settings,
        AgentRunRepository(session),
        ToolRepository(session),
        ToolExecutionRepository(session),
        pipeline,
        gateway.provider_for_model,
        retrieval,
        # Built once at startup and shared, because it owns a queue and a background
        # drain task. None unless LANGFUSE__ENABLED (M19).
        getattr(request.app.state, "langfuse", None),
    )


def get_agent_run_service(
    session: SessionDep,
    registry: AgentRegistryDep,
    runtime: Annotated[PlatformAgentRuntime, Depends(get_agent_runtime)],
    audit: AuditDep,
) -> AgentRunService:
    return AgentRunService(
        AgentRunRepository(session),
        AgentRunEventRepository(session),
        ToolExecutionRepository(session),
        AgentVersionRepository(session),
        registry,
        runtime,
        audit,
    )


AgentRunServiceDep = Annotated[AgentRunService, Depends(get_agent_run_service)]


# ---------------------------------------------------------------------------
# Construction outside a request (M29)
# ---------------------------------------------------------------------------
def build_agent_run_service(session: AsyncSession, app_state: Any) -> AgentRunService:
    """The agent stack, assembled without FastAPI.

    The voice WebSocket needs exactly what a request gets, but it has no `Request` and no
    dependency resolution — and a second, hand-rolled assembly would drift from this one.
    The first thing to drift would be the §10 pipeline, which is the last thing that
    should differ between typing a question and speaking it.

    So this is the single construction path: the dependencies below call it, and so does
    `app.api.voice_ws`.
    """
    settings = get_settings()
    audit = AuditService(AuditRepository(session), session_factory=app_state.database.sessionmaker)
    pipeline = ToolPipeline(
        settings,
        ToolExecutionRepository(session),
        audit,
        app_state.tool_executors,
    )
    gateway = GatewayService(
        settings,
        ModelRepository(session),
        ModelAliasRepository(session),
        ModelDeploymentRepository(session),
        ApiKeyRepository(session),
        UsageRepository(session),
        app_state.redis.client,
        app_state.database.sessionmaker,
    )
    knowledge = KnowledgeService(
        settings,
        KnowledgeBaseRepository(session),
        DocumentRepository(session),
        DocumentChunkRepository(session),
        EmbeddingModelRepository(session),
        QdrantVectorStore(app_state.qdrant.client),
        audit,
        _embed_through(gateway),
        app_state.minio,
        ResolvingOcrEngine(gateway.ocr_engine_for_model),
    )
    memory = MemoryService(
        ConversationRepository(session),
        ConversationMessageRepository(session),
        MemoryEntryRepository(session),
        QdrantVectorStore(app_state.qdrant.client),
        app_state.redis.client,
        _embed_through(gateway),
        settings.knowledge.memory_embedding_model,
    )
    retrieval = RetrievalService(
        knowledge, memory, KnowledgeBaseRepository(session), settings.knowledge.default_tenant
    )
    runtime = PlatformAgentRuntime(
        settings,
        AgentRunRepository(session),
        ToolRepository(session),
        ToolExecutionRepository(session),
        pipeline,
        gateway.provider_for_model,
        retrieval,
        getattr(app_state, "langfuse", None),
    )
    registry = AgentRegistryService(
        settings,
        AgentRepository(session),
        AgentVersionRepository(session),
        ToolRepository(session),
        SkillRepository(session),
        audit,
    )
    return AgentRunService(
        AgentRunRepository(session),
        AgentRunEventRepository(session),
        ToolExecutionRepository(session),
        AgentVersionRepository(session),
        registry,
        runtime,
        audit,
    )


def _embed_through(gateway: GatewayService) -> Any:
    """Embedding callable bound to one gateway — the same route a request would take."""

    async def embed(texts: list[str], model: str) -> list[list[float]]:
        # Served name, not the alias the caller passed: upstream has never heard of it.
        provider, served_model = await gateway.provider_for_model(model)
        result = await provider.embeddings(texts, model=served_model)
        return [list(vector) for vector in result.vectors]

    return embed


def get_voice_config_store(session: SessionDep, settings: SettingsDep) -> VoiceConfigStore:
    return VoiceConfigStore(session, settings)


VoiceConfigStoreDep = Annotated[VoiceConfigStore, Depends(get_voice_config_store)]


async def get_voice_service(session: SessionDep, store: VoiceConfigStoreDep) -> VoiceSessionService:
    """Built on the *effective* configuration, not on the environment.

    Resolved per request because an administrator can change it at run time (§49): a
    service holding the boot-time settings would keep pointing at yesterday's model until
    the container restarted, which is exactly what run-time configuration exists to avoid.
    """
    return VoiceSessionService(
        await store.get(),
        VoiceSessionRepository(session),
        VoiceMessageRepository(session),
        VoiceEventRepository(session),
        AgentRepository(session),
    )


VoiceServiceDep = Annotated[VoiceSessionService, Depends(get_voice_service)]


def get_user_repository(session: SessionDep) -> UserRepository:
    """A repository, exceptionally, for the gateway's agent route.

    Routers depend on services (Rule 6), and this is the one place that bends: the
    gateway needs to resolve an API client's owner to authorise an agent run, and
    wrapping a single `get by id` in a service would be ceremony. Read-only, one
    call, and the layering contract permits repositories from api.deps precisely
    because deps.py *is* the composition seam.
    """
    return UserRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]


# ---------------------------------------------------------------------------
# Knowledge and memory (M15, M16)
# ---------------------------------------------------------------------------
def get_vector_store(request: Request) -> VectorStore:
    """The §28 VectorStore over the shared Qdrant client."""
    return QdrantVectorStore(request.app.state.qdrant.client)


def get_embedder(gateway: GatewayServiceDep) -> Any:
    """Embeds texts through the **gateway**, not directly against a runtime.

    So a knowledge base uses the same alias resolution and the same deployment a
    developer's API call would (§13). A second embedding path would eventually disagree
    with the first about which model is serving, and the symptom would be a knowledge base
    whose vectors are silently incomparable with its own queries.
    """

    async def embed(texts: list[str], model: str) -> list[list[float]]:
        # Served name, not the alias the caller passed: upstream has never heard of it.
        provider, served_model = await gateway.provider_for_model(model)
        result = await provider.embeddings(texts, model=served_model)
        return [list(vector) for vector in result.vectors]

    return embed


def get_knowledge_service(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    audit: AuditDep,
    embed: Annotated[Any, Depends(get_embedder)],
    gateway: GatewayServiceDep,
) -> KnowledgeService:
    return KnowledgeService(
        settings,
        KnowledgeBaseRepository(session),
        DocumentRepository(session),
        DocumentChunkRepository(session),
        EmbeddingModelRepository(session),
        get_vector_store(request),
        audit,
        embed,
        request.app.state.minio,
        # OCR through the gateway too (M28), for the same reason embedding goes that way.
        ResolvingOcrEngine(gateway.ocr_engine_for_model),
    )


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def get_memory_service(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    embed: Annotated[Any, Depends(get_embedder)],
) -> MemoryService:
    return MemoryService(
        ConversationRepository(session),
        ConversationMessageRepository(session),
        MemoryEntryRepository(session),
        get_vector_store(request),
        request.app.state.redis.client,
        embed,
        settings.knowledge.memory_embedding_model,
    )


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


def get_retrieval_service(
    session: SessionDep,
    settings: SettingsDep,
    knowledge: KnowledgeServiceDep,
    memory: MemoryServiceDep,
) -> RetrievalService:
    return RetrievalService(
        knowledge, memory, KnowledgeBaseRepository(session), settings.knowledge.default_tenant
    )


RetrievalServiceDep = Annotated[RetrievalService, Depends(get_retrieval_service)]


def get_dashboard_service(session: SessionDep) -> DashboardService:
    return DashboardService(
        NodeRepository(session),
        GpuRepository(session),
        ModelRepository(session),
        ModelDeploymentRepository(session),
        UsageRepository(session),
        AuditRepository(session),
    )


DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]


def get_monitoring_service(session: SessionDep, settings: SettingsDep) -> MonitoringService:
    return MonitoringService(session, settings)


MonitoringServiceDep = Annotated[MonitoringService, Depends(get_monitoring_service)]


def get_trace_service(session: SessionDep, settings: SettingsDep) -> TraceService:
    return TraceService(
        AgentRunRepository(session),
        AgentRunEventRepository(session),
        settings,
    )


TraceServiceDep = Annotated[TraceService, Depends(get_trace_service)]


def get_definition_importer(
    session: SessionDep,
    settings: SettingsDep,
    tools: ToolRegistryDep,
    skills: SkillRegistryDep,
    agents: AgentRegistryDep,
) -> DefinitionImporter:
    """Imports the shipped agent, skill and tool definitions (M10-M12).

    Built on the registry services rather than the repositories, so an imported agent
    goes through exactly the same validation, versioning and audit as one created through
    the API. A second write path would eventually diverge, and the manifests are the copy
    an air-gapped site actually gets.
    """
    return DefinitionImporter(
        settings,
        ToolRepository(session),
        SkillRepository(session),
        AgentRepository(session),
        tools,
        skills,
        agents,
    )


DefinitionImporterDep = Annotated[DefinitionImporter, Depends(get_definition_importer)]


def get_api_key_service(request: Request, session: SessionDep, audit: AuditDep) -> ApiKeyService:
    return ApiKeyService(
        ApiClientRepository(session),
        ApiKeyRepository(session),
        UsageRepository(session),
        audit,
        request.app.state.secret_cipher,
    )


ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]


def get_gateway_service(
    request: Request, session: SessionDep, settings: SettingsDep
) -> GatewayService:
    return GatewayService(
        settings,
        ModelRepository(session),
        ModelAliasRepository(session),
        ModelDeploymentRepository(session),
        ApiKeyRepository(session),
        UsageRepository(session),
        request.app.state.redis.client,
        request.app.state.database.sessionmaker,
    )


GatewayServiceDep = Annotated[GatewayService, Depends(get_gateway_service)]


async def get_gateway_context(
    request: Request,
    gateway: GatewayServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> GatewayContext:
    """Authenticate a gateway call with an API key and apply its rate limit.

    Deliberately not the platform JWT: these are machine-to-machine calls from developer
    applications, which need a long-lived credential revocable independently of any human
    account. The same `Authorization: Bearer` header carries it, so the stock OpenAI SDK
    works unmodified.
    """
    if credentials is None or not credentials.credentials:
        raise TokenError(
            "An API key is required. Create one at POST /api/v1/api-keys and send it as "
            "'Authorization: Bearer <key>'."
        )

    key = await gateway.authenticate(credentials.credentials)
    await gateway.check_rate_limit(key)

    # Identity forwarded by a shared frontend (M17). See app/services/identity.py for why
    # this is gated per client rather than simply read from the header.
    settings: Settings = request.app.state.settings
    end_user = resolve_forwarded_identity(
        request.headers, key.client, settings.gateway, request.app.state.secret_cipher
    )

    return GatewayContext(
        api_key=key,
        end_user=end_user.subject if end_user else None,
        end_user_trusted=end_user.trusted if end_user else False,
    )


GatewayContextDep = Annotated[GatewayContext, Depends(get_gateway_context)]
