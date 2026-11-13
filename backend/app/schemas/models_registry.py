"""Model registry, deployment, alias and API-key schemas (M07-M09, M20)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

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
    license: str | None = Field(
        default=None, max_length=128, description="SPDX id where there is one."
    )
    source_reference: str | None = Field(
        default=None,
        max_length=512,
        description="Where the weights came from. Provenance only; never fetched.",
    )
    publisher: str | None = Field(default=None, max_length=128)
    family: str | None = Field(
        default=None,
        max_length=128,
        description="Groups versions and quantised variants of the same model.",
    )
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=32
    )
    team_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Owning team (E3). Omit for a shared resource, visible to everyone with the "
            "permission. You must be a member of the team unless you hold scope.all."
        ),
    )


ApprovalStatus = Literal["approved", "experimental", "deprecated", "blocked"]


class ModelUpdateRequest(BaseModel):
    """Catalogue metadata (E4). Omitted fields are left unchanged; ``null`` clears one.

    Not the approval status, which has its own route and permission, and not anything
    that changes how the model is served (runtime, paths, GPU needs): those belong to
    a new registration or a deployment revision.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    version: str | None = Field(default=None, min_length=1, max_length=64)
    license: str | None = Field(
        default=None, max_length=128, description="SPDX id where there is one."
    )
    source_reference: str | None = Field(
        default=None,
        max_length=512,
        description="Where the weights came from. Provenance only; never fetched.",
    )
    publisher: str | None = Field(default=None, max_length=128)
    family: str | None = Field(
        default=None,
        max_length=128,
        description="Groups versions and quantised variants of the same model.",
    )
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=32
    )


class ModelApprovalRequest(BaseModel):
    status: ApprovalStatus
    note: str | None = Field(
        default=None,
        max_length=2000,
        description="Why. Shown next to the status and kept in the audit log.",
    )


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
    #: Owning team (E3); null means shared.
    team_id: uuid.UUID | None = None
    #: Governance (E4).
    approval_status: str = "approved"
    approval_note: str | None = None
    approved_by: uuid.UUID | None = None
    approved_at: dt.datetime | None = None
    license: str | None = None
    source_reference: str | None = None
    publisher: str | None = None
    family: str | None = None
    tags: list[str] = Field(default_factory=list)
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
    #: The configuration revision now applied (E4).
    current_revision: int = 1
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
# Deploy validation and revisions (E4, plan §5.4)
# ---------------------------------------------------------------------------
class PlacementPreview(BaseModel):
    node_id: uuid.UUID
    node_name: str | None = None
    gpu_indices: list[int]
    rationale: str


class DeployValidationResponse(BaseModel):
    """A dry run of `POST /models/{id}/deploy`: nothing is reserved or started.

    The placement is what the scheduler would choose *now*. Another deployment can take
    the GPUs before the real request arrives, so the real request re-checks everything.
    """

    ok: bool
    model: str
    runtime: str
    external: bool
    approval_status: str
    placement: PlacementPreview | None = None
    #: The configuration the deployment would start with, defaults filled in.
    config: dict[str, Any] = Field(default_factory=dict)
    #: Figures taken from the model's catalogue entry, not measured. Labelled so nobody
    #: reads them as observed usage.
    estimates: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DeploymentRevisionRead(ORMModel):
    id: uuid.UUID
    deployment_id: uuid.UUID
    revision: int
    config: dict[str, Any]
    change_note: str | None = None
    rolled_back_from: int | None = None
    created_by: uuid.UUID | None = None
    created_at: dt.datetime
    #: Filled in by the route: whether this revision is the one now applied.
    current: bool = False


class DeploymentRevisionRequest(BaseModel):
    """Change how a managed deployment runs. Omitted fields keep their current value.

    The GPU set is not changeable here. The reservation is kept across the change so
    nothing else can take the devices; a different GPU count is a new deployment.
    """

    image: str | None = Field(
        default=None,
        max_length=512,
        description=("The runtime's configured image or one in MODELS__RUNTIME_IMAGE_ALLOWLIST."),
    )
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=16)
    max_model_len: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float | None = Field(default=None, gt=0.0, le=1.0)
    change_note: str | None = Field(default=None, max_length=2000)


class DeploymentRollbackRequest(BaseModel):
    revision: int = Field(ge=1)
    change_note: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Aliases (§13)
# ---------------------------------------------------------------------------
class AliasCreateRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    model_id: uuid.UUID
    description: str | None = None
    enabled: bool = True
    #: Alias to serve from when this one's model cannot (E2). Explicit, one hop.
    fallback_alias_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Owning team (E3). Omit for a shared resource, visible to everyone with the "
            "permission. You must be a member of the team unless you hold scope.all."
        ),
    )


class AliasUpdateRequest(BaseModel):
    """Repointing an alias is the whole point of §13 — it swaps the model behind a
    stable public name without any developer application changing."""

    model_id: uuid.UUID | None = None
    description: str | None = None
    enabled: bool | None = None
    #: Send ``null`` to remove the fallback; omit the field to leave it unchanged.
    fallback_alias_id: uuid.UUID | None = None


class AliasRead(ORMModel):
    id: uuid.UUID
    alias: str
    model_id: uuid.UUID
    description: str | None = None
    enabled: bool
    fallback_alias_id: uuid.UUID | None = None
    #: Owning team (E3); null means shared.
    team_id: uuid.UUID | None = None
    created_at: dt.datetime


class AliasDetail(AliasRead):
    # Derived, like DeploymentDetail's — see the note there.
    model_name: str = ""
    serving: bool = False
    fallback_alias: str | None = None


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
    team_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Owning team (E3). The client's keys can then call this team's models, aliases "
            "and agents as well as shared ones; usage and team quotas are charged to it."
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
    #: Owning team (E3); null means shared.
    team_id: uuid.UUID | None = None
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
    #: Calls that ended in an error status (E2).
    errors: int = 0
    #: Calls this model answered as an alias's fallback (E2).
    fallbacks: int = 0
    #: Timing percentiles (E16). None when nothing in range was measured: TTFT and
    #: throughput come from streamed calls only, and `timed_streams` says how many.
    ttft_p50_ms: float | None = None
    ttft_p95_ms: float | None = None
    latency_p95_ms: float | None = None
    output_tps_p50: float | None = None
    timed_streams: int = 0


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
