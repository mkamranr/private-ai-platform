"""Nodes, GPUs, containers and GPU allocations (M04, M05, M06)."""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
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


class NodeStatus(enum.StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    # Reachable but something is wrong — Docker down, GPU probe failing. Distinct
    # from OFFLINE because the platform can still read telemetry and an operator
    # needs to see the difference.
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class NodeRole(enum.StrEnum):
    GPU = "GPU"
    CPU = "CPU"


class ContainerState(enum.StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    RESTARTING = "RESTARTING"
    PAUSED = "PAUSED"
    EXITED = "EXITED"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


class GpuHealth(enum.StrEnum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class Node(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A managed host running the node agent (§M04)."""

    __tablename__ = "nodes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ONLINE', 'OFFLINE', 'DEGRADED', 'UNKNOWN')", name="status_valid"
        ),
        CheckConstraint("role IN ('GPU', 'CPU')", name="role_valid"),
    )

    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(45))

    # Where the control plane reaches the agent, e.g. https://gpu-node-01:9100.
    agent_url: Mapped[str] = mapped_column(String(512), nullable=False)

    # The agent's bearer token, Fernet-encrypted under SECURITY__ENCRYPTION_KEY
    # (`app.core.security.SecretCipher`, built in Phase 0 for exactly this class of
    # secret).
    #
    # Encrypted rather than hashed, because the platform *presents* this token — it
    # never verifies an incoming one, so a hash would be unusable. Encryption keeps
    # the property that matters: the key is mounted from outside the database, so a
    # database dump alone does not yield control of every managed host.
    #
    # An in-memory cache was the obvious alternative and is wrong: node polling would
    # silently stop after every control-plane restart until each node was manually
    # re-registered, and a fleet that quietly stops being monitored is worse than one
    # that visibly fails.
    agent_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(32))
    # Whether the control plane verifies the agent's TLS certificate. False is only
    # acceptable on a private container network (§M04).
    agent_verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    role: Mapped[str] = mapped_column(String(8), default=NodeRole.CPU, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=NodeStatus.UNKNOWN, nullable=False, index=True
    )
    status_detail: Mapped[str | None] = mapped_column(Text)

    os_info: Mapped[str | None] = mapped_column(String(255))
    kernel_version: Mapped[str | None] = mapped_column(String(255))
    architecture: Mapped[str | None] = mapped_column(String(32))
    cpu_model: Mapped[str | None] = mapped_column(String(255))
    cpu_cores: Mapped[int | None] = mapped_column(Integer)
    memory_total_mib: Mapped[int | None] = mapped_column(Integer)

    docker_version: Mapped[str | None] = mapped_column(String(64))
    # Presence of the nvidia container runtime. The platform must not schedule a GPU
    # workload onto a host that reports GPUs but cannot expose them to a container —
    # a mismatch that otherwise surfaces as a container that starts and immediately
    # fails to see any device.
    nvidia_runtime_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    nvidia_driver_version: Mapped[str | None] = mapped_column(String(64))
    cuda_version: Mapped[str | None] = mapped_column(String(32))
    gpu_probe: Mapped[str | None] = mapped_column(String(32))
    # True when the node reports synthetic GPU telemetry. Surfaced prominently so a
    # fake node can never be mistaken for real capacity.
    gpu_synthetic: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    labels: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    gpus: Mapped[list[Gpu]] = relationship(
        back_populates="node", cascade="all, delete-orphan", lazy="selectin"
    )
    containers: Mapped[list[Container]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Node {self.name} {self.status}>"


class EnrollmentStatus(enum.StrEnum):
    """Where a node enrolment got to (M04)."""

    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class NodeEnrollment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A pending invitation for a host to join the fleet (M04).

    **A table of its own rather than a PENDING node**, because four existing consumers
    treat a row in `nodes` as a contactable host and would each be wrong about a
    half-enrolled one: the poller hands every non-disabled node to a client builder, the
    staleness sweep would flip it to OFFLINE with "never reported since registration", the
    dashboard's fleet counts would stop summing, and `agent_url`/`agent_token_encrypted`
    would have to become nullable — which makes the migration's downgrade impossible to
    write without deleting rows.

    It also models re-enrolment, which a status on `nodes` cannot: a host being rebuilt
    already has an ONLINE row, and `node_id` below points at it so the token is rotated in
    place and its GPUs, metrics and deployments survive.

    The token is stored as a **hash**. The platform verifies what a node presents, so a
    one-way hash suffices and a database dump yields nothing usable — the mirror image of
    `Node.agent_token_encrypted`, which must stay reversible because the platform presents
    that one on every poll.
    """

    __tablename__ = "node_enrollments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'CONSUMED', 'EXPIRED', 'REVOKED')",
            name="enrollment_status_valid",
        ),
        Index("ix_node_enrollments_token_hash", "token_hash", unique=True),
        # At most one live invitation per name, enforced where it cannot race. This is why
        # expiry must actively flip rows out of PENDING: an expired-but-unswept row would
        # otherwise block re-issuing for that name for ever.
        Index(
            "uq_node_enrollments_pending_name",
            "node_name",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_node_enrollments_status_expires", "status", "expires_at"),
    )

    # The identity the node will take. Deliberately not something the enrolling node can
    # choose: a compromised host re-running the script cannot adopt another node's name.
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Set by the administrator at issue time. Never by the enrolling node — that would be
    # a TLS downgrade the caller controls.
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Set only for re-enrolment: which existing node this rotates.
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )

    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=EnrollmentStatus.PENDING, nullable=False, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 45 characters fits IPv6, matching AuditLog.source_ip.
    consumed_from_ip: Mapped[str | None] = mapped_column(String(45))
    # Kept for forensics: what the node claimed, and what that resolved to at probe time.
    # The pair is what makes a DNS rebind visible after the fact.
    advertised_url: Mapped[str | None] = mapped_column(String(512))
    resolved_ip: Mapped[str | None] = mapped_column(String(45))

    # Bounds how many outbound probes one token can cause. In Postgres rather than Redis
    # so it survives a cache outage and cannot be evaded by rotating source IP.
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NodeEnrollment {self.node_name} {self.status}>"


class Gpu(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A physical GPU on a node (§M05)."""

    __tablename__ = "gpus"
    __table_args__ = (
        # The hardware UUID is the stable identity: `index` is reassigned when a card
        # is moved between slots, so keying on it would silently merge two devices'
        # metric history.
        UniqueConstraint("uuid", name="uuid"),
        UniqueConstraint("node_id", "index", name="node_index"),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    uuid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    driver_version: Mapped[str | None] = mapped_column(String(64))
    cuda_version: Mapped[str | None] = mapped_column(String(32))
    pci_bus_id: Mapped[str | None] = mapped_column(String(64))
    # Peer indices reachable over NVLink. Unused by the V1 scheduler; §9's
    # topology-aware placement needs it, so it is collected from the start.
    nvlink_peers: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=GpuHealth.UNKNOWN, nullable=False)

    node: Mapped[Node] = relationship(back_populates="gpus")

    def __repr__(self) -> str:
        return f"<Gpu {self.index} {self.name}>"


class GpuMetric(Base):
    """One telemetry sample (§M05).

    The platform's highest-volume table: four GPUs at a 15-second interval is roughly
    700k rows per month per node. No UUID primary key and no ``updated_at`` — both
    would add bytes per row for no query anyone runs. Retention is enforced by a
    scheduled job (``GPU__METRIC_RETENTION_DAYS``), not left to grow.
    """

    __tablename__ = "gpu_metrics"
    __table_args__ = (
        # Serves the only query shape that matters: this GPU, most recent first.
        Index("ix_gpu_metrics_gpu_recorded", "gpu_id", "recorded_at"),
        # Retention deletes by age across all GPUs.
        Index("ix_gpu_metrics_recorded_at", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gpu_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("gpus.id", ondelete="CASCADE"), nullable=False
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    utilization_percent: Mapped[float] = mapped_column(Float, nullable=False)
    memory_used_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature_celsius: Mapped[float] = mapped_column(Float, nullable=False)
    power_draw_watts: Mapped[float] = mapped_column(Float, nullable=False)
    power_limit_watts: Mapped[float | None] = mapped_column(Float)
    sm_clock_mhz: Mapped[int | None] = mapped_column(Integer)
    sm_utilization_percent: Mapped[float | None] = mapped_column(Float)
    # Nullable, never defaulted to 0: a device that does not report ECC must not look
    # like a device reporting zero errors.
    ecc_errors_corrected: Mapped[int | None] = mapped_column(Integer)
    ecc_errors_uncorrected: Mapped[int | None] = mapped_column(Integer)
    pcie_replay_counter: Mapped[int | None] = mapped_column(Integer)
    nvlink_bandwidth_mbps: Mapped[float | None] = mapped_column(Float)
    health: Mapped[str] = mapped_column(String(16), default=GpuHealth.UNKNOWN, nullable=False)


class GpuProcess(Base):
    """A process observed holding GPU memory (§M05).

    Kept as current-state, not history: rows for a GPU are replaced wholesale each
    collection cycle. Historical process occupancy has no consumer, and retaining it
    would make this table grow like `gpu_metrics` for no benefit.
    """

    __tablename__ = "gpu_processes"
    __table_args__ = (Index("ix_gpu_processes_gpu", "gpu_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gpu_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("gpus.id", ondelete="CASCADE"), nullable=False
    )
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    process_name: Mapped[str] = mapped_column(String(255), nullable=False)
    used_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    # Correlates actual occupancy back to a deployment, which is how the platform
    # detects a GPU busy with something it did not schedule.
    container_id: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GpuHealthEvent(UUIDPrimaryKeyMixin, Base):
    """A health transition worth an operator's attention (§M05).

    Recorded on *change* rather than per sample: an alert stream that repeats
    "GPU 3 is hot" every 15 seconds is one an operator learns to ignore.
    """

    __tablename__ = "gpu_health_events"
    __table_args__ = (
        Index("ix_gpu_health_events_gpu_occurred", "gpu_id", "occurred_at"),
        CheckConstraint(
            "severity IN ('HEALTHY', 'WARNING', 'CRITICAL', 'UNKNOWN')", name="severity_valid"
        ),
    )

    gpu_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("gpus.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_severity: Mapped[str | None] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class GpuAllocation(UUIDPrimaryKeyMixin, Base):
    """A GPU reserved for a workload — **not in the spec, added deliberately**.

    Without it, two concurrent deploy requests can both observe GPUs 0 and 1 as free
    and both claim them. The first vLLM container wins; the second dies with a CUDA
    OOM that reads like a model problem and costs an afternoon to diagnose.

    The partial unique index below makes the second claim fail *at the database*,
    which is the only place the check can be race-free. ``Scheduler.reserve()`` must
    take the reservation in the same transaction that creates the deployment row.

    Released allocations are retained rather than deleted: "which GPUs did this
    deployment hold, and when?" is a question incident review actually asks.
    """

    __tablename__ = "gpu_allocations"
    __table_args__ = (
        # The heart of the fix. A GPU may have many historical allocations but at most
        # one live one, and PostgreSQL enforces that rather than application code.
        #
        # A plain UNIQUE(node_id, gpu_index) would be wrong: it would forbid ever
        # reusing a GPU after release. The WHERE clause scopes uniqueness to active
        # rows, which is exactly the invariant that matters.
        Index(
            "uq_gpu_allocations_active",
            "node_id",
            "gpu_index",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_gpu_allocations_node_active", "node_id", "released_at"),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("gpus.id", ondelete="SET NULL")
    )
    # Groups the GPUs of one placement, so a tensor-parallel deployment's devices are
    # reserved and released as a unit.
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    # Free text in Phase 1; becomes an FK to model_deployments in Phase 2, once that
    # table exists.
    purpose: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    reserved_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reserved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    @property
    def is_active(self) -> bool:
        return self.released_at is None


class Container(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A container observed on a node (§M06).

    A cache of what the node agent reports, refreshed by the inventory worker. The
    Docker daemon remains the source of truth; this table exists so the admin UI and
    the API can answer without a round trip to every node.
    """

    __tablename__ = "containers"
    __table_args__ = (
        UniqueConstraint("node_id", "container_id", name="node_container"),
        Index("ix_containers_node_state", "node_id", "state"),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default=ContainerState.UNKNOWN, nullable=False)
    status_text: Mapped[str | None] = mapped_column(String(255))
    labels: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    ports: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Whether the platform created it, and therefore whether it may control it.
    managed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    node: Mapped[Node] = relationship(back_populates="containers")

    def __repr__(self) -> str:
        return f"<Container {self.name} {self.state}>"
