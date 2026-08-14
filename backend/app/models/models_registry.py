"""Model registry, deployments, aliases and API usage (M07, M08, M09, §12, §13)."""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ModelType(enum.StrEnum):
    """§M07 model types."""

    LLM = "LLM"
    EMBEDDING = "EMBEDDING"
    RERANKER = "RERANKER"
    ASR = "ASR"
    TTS = "TTS"
    OCR = "OCR"
    VISION = "VISION"
    MULTIMODAL = "MULTIMODAL"


class ModelStatus(enum.StrEnum):
    # Catalogued, but the weights are not on disk yet — the common state on a fresh
    # air-gapped install where models arrive separately on physical media.
    REGISTERED = "REGISTERED"
    # Files present and checksums verified. Only these may be deployed.
    AVAILABLE = "AVAILABLE"
    # Files missing or a checksum mismatch.
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"


class DeploymentState(enum.StrEnum):
    """The §M08 lifecycle.

    Driven by a background worker, never inside a request: loading a 30B model takes
    minutes, so ``POST /models/{id}/deploy`` returns 202 and the client polls.
    """

    REQUESTED = "REQUESTED"
    VALIDATING = "VALIDATING"
    SCHEDULING = "SCHEDULING"
    CREATING = "CREATING"
    STARTING = "STARTING"
    HEALTH_CHECK = "HEALTH_CHECK"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


#: States from which no further transition happens without a new request.
TERMINAL_STATES = frozenset(
    {DeploymentState.RUNNING, DeploymentState.FAILED, DeploymentState.STOPPED}
)
#: States where the deployment holds GPUs and a container.
ACTIVE_STATES = frozenset(
    {
        DeploymentState.CREATING,
        DeploymentState.STARTING,
        DeploymentState.HEALTH_CHECK,
        DeploymentState.RUNNING,
    }
)


class Model(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A model in the local catalogue (§M07)."""

    __tablename__ = "models"
    __table_args__ = (
        CheckConstraint(
            "type IN ('LLM','EMBEDDING','RERANKER','ASR','TTS','OCR','VISION','MULTIMODAL')",
            name="type_valid",
        ),
        CheckConstraint(
            "status IN ('REGISTERED','AVAILABLE','UNAVAILABLE','DISABLED')", name="status_valid"
        ),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), default="1.0", nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    architecture: Mapped[str | None] = mapped_column(String(64))
    # BigInteger: a 235B model overflows a 32-bit int.
    parameter_count: Mapped[int | None] = mapped_column(BigInteger)
    quantization: Mapped[str | None] = mapped_column(String(32))
    context_length: Mapped[int | None] = mapped_column(Integer)

    # Absolute path on the node. Never a URL — the platform does not download models
    # (Rule 4); they arrive on physical media and are catalogued from disk.
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    runtime: Mapped[str] = mapped_column(String(32), default="vllm", nullable=False)

    @property
    def served_model_name(self) -> str:
        """What the runtime serving this model calls it.

        Usually the platform's own name — a container the platform started is launched
        with `--served-model-name`, so the two agree by construction.

        An external runtime is different: it was already running with its own naming when
        the platform found it, and the platform's name is a *sanitised* version of it.
        Ollama's `llama3.2:latest` cannot be a platform model name (the colon and dots are
        invalid), so it is stored on import and sent back verbatim here. Without this the
        gateway asks Ollama for `llama3-2-latest` and gets a 404 naming a model nobody
        registered under that name.
        """
        original = (self.metadata_json or {}).get("ollama_tag")
        if original:
            return str(original)
        if self.storage_path.startswith("ollama://"):
            return self.storage_path.removeprefix("ollama://")
        # The same problem one step further out: a hosted provider names models
        # `vendor/model-name:variant`, and the slash and colon are no more valid in a
        # platform model name than Ollama's were. Asking OpenRouter for
        # `nvidia-nemotron-3-ultra-550b-a55b-free` returns a 404 for a model nobody has.
        if self.storage_path.startswith("external://"):
            return self.storage_path.removeprefix("external://")
        return self.name

    #: Where an **external** runtime already serves this model (M07). Set only for the
    #: runtimes in EXTERNAL_RUNTIMES; null for anything the platform deploys itself,
    #: where the address is a property of the container the platform created.
    endpoint_url: Mapped[str | None] = mapped_column(String(512))

    # What the scheduler needs to place it (§9).
    required_gpu_memory_mib: Mapped[int | None] = mapped_column(Integer)
    min_gpu_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supported_gpu: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[str] = mapped_column(
        String(16), default=ModelStatus.REGISTERED, nullable=False, index=True
    )
    status_detail: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    # Runtime arguments from the manifest, e.g. extra vLLM flags.
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    files: Mapped[list[ModelFile]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    deployments: Mapped[list[ModelDeployment]] = relationship(back_populates="model")

    def __repr__(self) -> str:
        return f"<Model {self.name} {self.status}>"


class ModelFile(Base):
    """One file belonging to a model, with its checksum (§M07).

    Checksums are verified on import. An air-gapped bundle arrives by physical media,
    and a truncated safetensors file otherwise surfaces as an inscrutable vLLM crash
    minutes into loading — by which point the operator is debugging the wrong thing.
    """

    __tablename__ = "model_files"
    __table_args__ = (UniqueConstraint("model_id", "relative_path", name="model_path"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # BigInteger: a single safetensors shard routinely exceeds 2 GiB.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Nullable: hashing a 60 GiB weights file takes minutes, so it is opt-in per import.
    sha256: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    model: Mapped[Model] = relationship(back_populates="files")


class ModelDeployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A model being served on a node (§M08)."""

    __tablename__ = "model_deployments"
    __table_args__ = (
        Index("ix_model_deployments_state", "state"),
        Index("ix_model_deployments_model_state", "model_id", "state"),
        # 0 is permitted and means **not applicable**: an external runtime (Ollama, or
        # anything else already serving) manages its own memory, and the platform has no
        # say in it. The upper bound still holds for everything the platform deploys —
        # a fraction above 1 is always a mistake.
        CheckConstraint(
            "gpu_memory_utilization >= 0 AND gpu_memory_utilization <= 1",
            name="gpu_memory_fraction",
        ),
    )

    # RESTRICT, not CASCADE: deleting a model that is still serving traffic must be
    # refused, not silently orphan a running container on some node.
    model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    #: Nullable since Phase 6: an **external** runtime (Ollama, or anything else already
    #: serving) runs somewhere the platform has no node record for. Every managed
    #: deployment still has one, and the service sets it — a null here means "the platform
    #: does not run this", which is exactly what it should mean.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True
    )

    state: Mapped[str] = mapped_column(
        String(16), default=DeploymentState.REQUESTED, nullable=False
    )
    state_detail: Mapped[str | None] = mapped_column(Text)

    gpu_indices: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Ties the deployment to its GPU claim. The reservation is taken in the *same
    # transaction* that creates this row, so a failure to create the deployment
    # releases the GPUs by rollback (§9).
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), index=True)

    runtime: Mapped[str] = mapped_column(String(32), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    container_id: Mapped[str | None] = mapped_column(String(64))
    container_name: Mapped[str | None] = mapped_column(String(255))

    internal_port: Mapped[int] = mapped_column(Integer, nullable=False)
    # Reachable only from inside the platform network. Never exposed to callers, who
    # reach models through a gateway alias instead (§12).
    internal_url: Mapped[str | None] = mapped_column(String(512))

    tensor_parallel_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_model_len: Mapped[int | None] = mapped_column(Integer)
    gpu_memory_utilization: Mapped[float] = mapped_column(Float, default=0.90, nullable=False)

    command: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    environment: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text)
    # Captured on failure. A model that fails to load produces the only evidence of why
    # in its container logs, and the container is usually gone by the time anyone looks.
    logs_excerpt: Mapped[str | None] = mapped_column(Text)

    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    healthy_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    model: Mapped[Model] = relationship(back_populates="deployments")

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def __repr__(self) -> str:
        return f"<ModelDeployment {self.model_id} {self.state}>"


class ModelAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A stable public name for a model (§13).

    The whole point of §12/§13: callers ask for ``enterprise-chat`` and never learn
    which model, deployment or container answered. Repointing the alias swaps the
    underlying model without touching a single developer application.
    """

    __tablename__ = "model_aliases"

    alias: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("models.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    model: Mapped[Model] = relationship()

    def __repr__(self) -> str:
        return f"<ModelAlias {self.alias}>"


class ApiClient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A developer application consuming the gateway (M20).

    Minimal in Phase 2 — the gateway is unusable without a credential, and §20's MVP
    scenario is explicit that "Developer creates API key" precedes calling it. The full
    developer portal lands in Phase 6.
    """

    __tablename__ = "api_clients"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # May this client tell the gateway *who* a request is for? (M17, §M24)
    #
    # A shared frontend like Open WebUI holds one service key on behalf of every user,
    # so without this every chat in the organisation accounts to a single identity. It
    # forwards the signed-in user instead — but a forwarded identity is just a string,
    # and honouring it from any key would let any developer bill their usage to someone
    # else. Off by default; granted deliberately, per client, to frontends the platform
    # itself deploys and whose users it authenticates.
    trusted_identity_headers: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Shared secret for verifying a *signed* forwarded identity (HS256).
    #
    # When set, only a valid signature is accepted and plaintext headers from this client
    # are ignored. That is the difference between "this identity is trusted because the
    # operator flagged the client" and "this identity is trusted because it is signed":
    # with a secret, stealing the API key is no longer enough to forge who a request
    # belongs to.
    #
    # Fernet-encrypted at rest, like node agent tokens, so a database dump alone does not
    # yield the ability to mint identities.
    identity_jwt_secret_encrypted: Mapped[str | None] = mapped_column(Text)

    keys: Mapped[list[ApiKey]] = relationship(back_populates="client", cascade="all, delete-orphan")


class ApiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A gateway credential.

    Only a SHA-256 hash and a visible prefix are stored; the full key is shown once, at
    creation. Storing it reversibly would make a database read equivalent to holding
    every developer's credential.

    SHA-256 rather than argon2 is correct here: the key is 256 bits of machine-generated
    entropy, so there is nothing to brute-force, and gateway auth must not pay an argon2
    cost on every inference request.
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_hash", "key_hash", unique=True),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_clients.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    #: What this key may do (M20). Two forms, and both must pass:
    #:
    #: * ``chat``, ``embeddings``, ``models`` — which gateway surfaces it may call.
    #: * ``model:<alias>`` — which aliases specifically.
    #:
    #: **Empty means unrestricted**, which is what every key minted before scopes existed
    #: is. Defaulting empty to "nothing" would silently break every running integration on
    #: upgrade; a key that grants everything is at least visible in the list.
    scopes: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)

    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when this key was superseded by a rotation, naming the key that replaced it.
    #: Kept so an operator seeing traffic on a key that should be dead can tell "still
    #: rotating" from "someone is using a credential we thought was gone".
    rotated_to: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    client: Mapped[ApiClient] = relationship(back_populates="keys")

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= dt.datetime.now(dt.UTC))


class UsageRecord(Base):
    """One gateway call, for accounting and quotas (M09, M20).

    High volume, like `gpu_metrics`: bigserial primary key, no `updated_at`, and
    indexed only on the dimensions anyone actually aggregates by.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        Index("ix_usage_records_recorded", "recorded_at"),
        Index("ix_usage_records_key_recorded", "api_key_id", "recorded_at"),
        Index("ix_usage_records_model_recorded", "model", "recorded_at"),
        # Partial: only rows carrying an end user are aggregated on this axis, and on a
        # table this size indexing the NULLs would be most of it for nothing.
        Index(
            "ix_usage_records_end_user_recorded",
            "end_user",
            "recorded_at",
            postgresql_where=text("end_user IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    # The alias the caller asked for, and the model that actually answered. Both are
    # needed: repointing an alias changes the second without the first, and usage
    # reports have to be able to show either view.
    requested_model: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    streamed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True when a streamed response ended early. Its token counts are a lower bound,
    # and a report that silently mixed those with complete ones would understate usage.
    client_disconnected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Who the call was *for*, behind a shared frontend (M17). Distinct from `user_id`,
    # which is a platform account: an Open WebUI user need not have one.
    end_user: Mapped[str | None] = mapped_column(String(255))
    # Whether that attribution can be relied on.
    #
    # Two sources reach `end_user` and they are not equally believable: a header from a
    # client the operator marked trusted, and the OpenAI-standard `user` body field,
    # which is whatever the caller typed. Both are worth keeping — self-reported tenant
    # tags are genuinely useful — but a chargeback report that silently mixed them would
    # bill people for traffic anyone could have attributed to them.
    end_user_trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
