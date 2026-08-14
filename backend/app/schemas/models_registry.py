"""Model registry, deployment, alias and API-key schemas (M07-M09, M20)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ---------------------------------------------------------------------------
# Models (M07)
# ---------------------------------------------------------------------------
class ModelRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    type: str = Field(
        default="LLM",
        description="LLM|EMBEDDING|RERANKER|ASR|TTS|OCR|VISION|MULTIMODAL",
    )
    storage_path: str = Field(
        default="",
        max_length=512,
        description=(
            "Absolute path on the node. Never a URL — the platform does not download "
            "models; they arrive with the offline bundle (Rule 4). Required for a runtime "
            "the platform starts; omit it for an external one, which has no local files."
        ),
    )
    runtime: str = Field(default="vllm", max_length=32)
    endpoint_url: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Required for an external runtime (ollama, external), forbidden otherwise. "
            "Where the model is already being served."
        ),
    )
    version: str = Field(default="1.0", max_length=64)
    architecture: str | None = Field(default=None, max_length=64)
    parameter_count: int | None = Field(default=None, ge=0)
    quantization: str | None = Field(default=None, max_length=32)
    context_length: int | None = Field(default=None, ge=1)
    required_gpu_memory_mib: int | None = Field(default=None, ge=0)
    min_gpu_count: int = Field(default=1, ge=0, le=16)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelFileRead(ORMModel):
    relative_path: str
    size_bytes: int
    sha256: str | None = None


class ModelRead(ORMModel):
    id: uuid.UUID
    name: str
    display_name: str
    version: str
    type: str
    architecture: str | None = None
    parameter_count: int | None = None
    quantization: str | None = None
    context_length: int | None = None
    storage_path: str
    runtime: str
    #: Set only for an external runtime. Its presence is how a reader tells a model the
    #: platform serves from one it merely points at.
    endpoint_url: str | None = None
    required_gpu_memory_mib: int | None = None
    min_gpu_count: int
    status: str
    status_detail: str | None = None
    description: str | None = None
    created_at: dt.datetime


class ModelDetail(ModelRead):
    files: list[ModelFileRead] = Field(default_factory=list)
    total_size_bytes: int = 0
    aliases: list[str] = Field(default_factory=list)
    active_deployments: int = 0


class ModelImportResponse(BaseModel):
    model_name: str
    status: str
    files_found: int
    files_hashed: int
    total_bytes: int
    detail: str | None = None


# ---------------------------------------------------------------------------
# Deployments (M08)
# ---------------------------------------------------------------------------
class DeployRequest(BaseModel):
    """The §M08 deployment payload."""

    node_id: uuid.UUID | None = Field(
        default=None, description="Omit to let the scheduler place it (§9)."
    )
    gpu_ids: list[int] | None = Field(
        default=None, description="Explicit devices. Omit to let the scheduler choose."
    )
    runtime: str | None = Field(default=None, max_length=32)
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=16)
    max_model_len: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float | None = Field(default=None, gt=0.0, le=1.0)


class DeploymentRead(ORMModel):
    id: uuid.UUID
    model_id: uuid.UUID
    #: Null for an external runtime: the platform routes to a server it does not run.
    node_id: uuid.UUID | None = None
    state: str
    state_detail: str | None = None
    gpu_indices: list[int] = Field(default_factory=list)
    runtime: str
    image: str
    container_id: str | None = None
    tensor_parallel_size: int
    max_model_len: int | None = None
    gpu_memory_utilization: float
    error_message: str | None = None
    started_at: dt.datetime | None = None
    healthy_at: dt.datetime | None = None
    stopped_at: dt.datetime | None = None
    created_at: dt.datetime
    # Deliberately NOT internal_url. §12: callers must never depend on a container
    # address, and exposing it here would invite exactly that.


class DeploymentDetail(DeploymentRead):
    # Defaulted because these are derived, not columns: the object is built with
    # model_validate() from the ORM row and the names are filled in afterwards. Without
    # a default, validation fails before they can be set.
    model_name: str = ""
    node_name: str | None = None


class DeploymentAcceptedResponse(BaseModel):
    """202 body.

    Deployment is asynchronous because loading a 30B model takes minutes — far longer
    than any sensible HTTP timeout. The caller polls `poll_url` for the §M08 lifecycle.
    """

    deployment_id: uuid.UUID
    state: str
    model: str
    #: Null for an **external** runtime — the platform points at a server it does not run,
    #: so there is no node of its own to name.
    node_id: uuid.UUID | None = None
    gpu_indices: list[int]
    poll_url: str
    message: str


class DeploymentLogsRead(BaseModel):
    deployment_id: uuid.UUID
    lines: str
    tail: int


# ---------------------------------------------------------------------------
# Aliases (§13)
# ---------------------------------------------------------------------------
class AliasCreateRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    model_id: uuid.UUID
    description: str | None = None
    enabled: bool = True


class AliasUpdateRequest(BaseModel):
    """Repointing an alias is the whole point of §13 — it swaps the model behind a
    stable public name without any developer application changing."""

    model_id: uuid.UUID | None = None
    description: str | None = None
    enabled: bool | None = None


class AliasRead(ORMModel):
    id: uuid.UUID
    alias: str
    model_id: uuid.UUID
    description: str | None = None
    enabled: bool
    created_at: dt.datetime


class AliasDetail(AliasRead):
    # Derived, like DeploymentDetail's — see the note there.
    model_name: str = ""
    serving: bool = False


# ---------------------------------------------------------------------------
# API keys (M20, minimal in Phase 2)
# ---------------------------------------------------------------------------
class ApiClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    trusted_identity_headers: bool = Field(
        default=False,
        description=(
            "Allow this client to tell the gateway who a request is for (M17). Grant only "
            "to frontends the platform deploys and whose users it authenticates — without "
            "a signing secret below, any holder of this client's key can attribute its "
            "usage to anyone."
        ),
    )
    identity_jwt_secret: str | None = Field(
        default=None,
        # RFC 7518 §3.2: an HS256 key should be at least as long as the hash output.
        # PyJWT warns below this, and a warning in a log is not a control — rejecting a
        # weak secret at the point it is set is.
        min_length=32,
        description=(
            "Shared HS256 secret. With one set, only a validly signed identity is "
            "accepted and plaintext headers from this client are ignored — so holding the "
            "API key is no longer enough to forge who a request belongs to. Stored "
            "encrypted and never returned."
        ),
    )


class ApiClientRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    enabled: bool
    trusted_identity_headers: bool
    #: Whether a signature is required — never the secret itself.
    identity_signature_required: bool = False
    created_at: dt.datetime


class ApiKeyCreateRequest(BaseModel):
    client_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    expires_at: dt.datetime | None = None
    scopes: list[str] = Field(
        default_factory=list,
        description=(
            "What this key may do. 'chat', 'embeddings', 'audio', 'models' restrict which "
            "gateway surfaces it can call; 'model:<alias>' restricts which aliases. Empty "
            "means unrestricted."
        ),
    )


class ApiKeyRotateRequest(BaseModel):
    grace_hours: int = Field(
        default=24,
        ge=0,
        le=720,
        description=(
            "How long the old key keeps working. Rotating with no overlap breaks every "
            "integration still holding the old key at that instant, which is why rotation "
            "gets avoided; 0 is available but deliberate."
        ),
    )


class ApiKeyRotateResponse(BaseModel):
    """The new key's plaintext, shown once — plus what happened to the old one."""

    api_key: str
    new_key: ApiKeyRead
    old_key_prefix: str
    old_key_expires_at: dt.datetime
    message: str


class ApiKeyRead(ORMModel):
    id: uuid.UUID
    client_id: uuid.UUID
    name: str
    prefix: str
    rate_limit_per_minute: int
    #: Empty means unrestricted — see the model. Shown so that is visible rather than
    #: inferred from an absent field.
    scopes: list[str] = Field(default_factory=list)
    expires_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    created_at: dt.datetime
    # Never key_hash, and never the key.


class ApiKeyCreatedResponse(BaseModel):
    """The only response that ever contains the key.

    Only a hash and a prefix are stored, so the platform genuinely cannot show it again
    — which is the property that makes a database read not equivalent to holding every
    developer's credential.
    """

    id: uuid.UUID
    name: str
    prefix: str
    api_key: str
    warning: str = "Copy this key now. Only its hash is stored, so it cannot be shown again."


# ---------------------------------------------------------------------------
# Usage (M09, M20)
# ---------------------------------------------------------------------------
class UsageSummaryRow(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    avg_latency_ms: float


class UsageSummaryResponse(BaseModel):
    since: dt.datetime
    rows: list[UsageSummaryRow]
    total_requests: int
    total_tokens: int


class EndUserUsageRow(BaseModel):
    end_user: str
    #: False means the identity is whatever the caller put in the request's `user` field.
    #: Surfaced rather than filtered out so a report cannot silently treat a self-reported
    #: label as an authenticated one.
    trusted: bool
    requests: int
    prompt_tokens: int
    completion_tokens: int
    last_seen_at: dt.datetime


class EndUserUsageResponse(BaseModel):
    since: dt.datetime
    rows: list[EndUserUsageRow]


class OllamaImportResponse(BaseModel):
    """One model found on an external Ollama (M07)."""

    name: str = Field(description="The platform's name for it — Ollama's tag, made safe.")
    ollama_tag: str = Field(description="What Ollama calls it, e.g. 'llama3.1:8b'.")
    status: str = Field(description="'registered' or 'already registered'.")
