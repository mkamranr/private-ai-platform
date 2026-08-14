"""Node, GPU and container schemas (M04-M06)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import ORMModel


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
class NodeRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    agent_url: str = Field(
        description="Base URL of the node agent, e.g. https://gpu-node-01:9100",
        max_length=512,
    )
    agent_token: str = Field(
        min_length=16,
        max_length=512,
        description="The token configured as NODE_AGENT_AUTH_TOKEN on that host.",
    )
    description: str | None = Field(default=None, max_length=1000)
    verify_tls: bool = Field(
        default=True,
        description=(
            "Verify the agent's TLS certificate. Set false only when the agent is "
            "reachable solely over a private container network (§M04)."
        ),
    )
    labels: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("agent_url must start with http:// or https://")
        return v.rstrip("/")


class GpuSummary(ORMModel):
    id: uuid.UUID
    index: int
    uuid: str
    name: str
    memory_total_mib: int
    status: str


class GpuRead(GpuSummary):
    node_id: uuid.UUID
    driver_version: str | None = None
    cuda_version: str | None = None
    pci_bus_id: str | None = None
    nvlink_peers: list[int] = Field(default_factory=list)
    created_at: dt.datetime


class NodeRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    hostname: str | None = None
    agent_url: str
    agent_version: str | None = None
    agent_verify_tls: bool
    role: str
    status: str
    status_detail: str | None = None
    os_info: str | None = None
    architecture: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    memory_total_mib: int | None = None
    docker_version: str | None = None
    nvidia_runtime_available: bool
    nvidia_driver_version: str | None = None
    cuda_version: str | None = None
    gpu_probe: str | None = None
    # Surfaced prominently: a node reporting synthetic telemetry must never be
    # mistaken for real capacity.
    gpu_synthetic: bool
    last_seen_at: dt.datetime | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
    gpus: list[GpuSummary] = Field(default_factory=list)
    # Never the agent token or its hash.


class NodeSyncResponse(BaseModel):
    node_name: str
    status: str
    gpus_seen: int = 0
    gpus_added: int = 0
    metrics_recorded: int = 0
    containers_seen: int = 0
    containers_removed: int = 0
    health_events: int = 0
    error: str | None = None


class NodeRegisterResponse(BaseModel):
    node: NodeRead
    sync: NodeSyncResponse


# ---------------------------------------------------------------------------
# GPU metrics
# ---------------------------------------------------------------------------
class GpuMetricRead(BaseModel):
    recorded_at: dt.datetime
    utilization_percent: float
    memory_used_mib: int
    memory_total_mib: int
    memory_utilization_percent: float
    temperature_celsius: float
    power_draw_watts: float
    power_limit_watts: float | None = None
    sm_clock_mhz: int | None = None
    ecc_errors_corrected: int | None = None
    ecc_errors_uncorrected: int | None = None
    health: str

    @classmethod
    def from_model(cls, metric: Any) -> GpuMetricRead:
        total = metric.memory_total_mib or 0
        return cls(
            recorded_at=metric.recorded_at,
            utilization_percent=metric.utilization_percent,
            memory_used_mib=metric.memory_used_mib,
            memory_total_mib=total,
            memory_utilization_percent=(
                round(metric.memory_used_mib / total * 100, 2) if total else 0.0
            ),
            temperature_celsius=metric.temperature_celsius,
            power_draw_watts=metric.power_draw_watts,
            power_limit_watts=metric.power_limit_watts,
            sm_clock_mhz=metric.sm_clock_mhz,
            ecc_errors_corrected=metric.ecc_errors_corrected,
            ecc_errors_uncorrected=metric.ecc_errors_uncorrected,
            health=metric.health,
        )


class GpuDetail(GpuRead):
    """A GPU plus its most recent sample, for the dashboard."""

    latest_metric: GpuMetricRead | None = None
    allocated: bool = False


class GpuMetricSeries(BaseModel):
    gpu_id: uuid.UUID
    gpu_index: int
    gpu_name: str
    samples: list[GpuMetricRead]


class GpuProcessRead(ORMModel):
    pid: int
    process_name: str
    used_memory_mib: int
    container_id: str | None = None
    observed_at: dt.datetime


class GpuHealthEventRead(ORMModel):
    id: uuid.UUID
    gpu_id: uuid.UUID
    event_type: str
    severity: str
    previous_severity: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    occurred_at: dt.datetime
    acknowledged_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# GPU allocations (§9)
# ---------------------------------------------------------------------------
class GpuAllocationRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID
    gpu_index: int
    reservation_id: uuid.UUID
    purpose: str
    reserved_at: dt.datetime
    released_at: dt.datetime | None = None


class GpuReserveRequest(BaseModel):
    node_id: uuid.UUID
    gpu_indices: list[int] = Field(min_length=1)
    purpose: str = Field(default="manual", max_length=255)


class GpuReserveResponse(BaseModel):
    reservation_id: uuid.UUID
    node_id: uuid.UUID
    gpu_indices: list[int]


class NodeCapacityRead(BaseModel):
    """What a node can currently accept — the scheduler's input in Phase 2."""

    node_id: uuid.UUID
    node_name: str
    status: str
    total_gpus: int
    free_gpu_indices: list[int]
    allocated_gpu_indices: list[int]


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
class ContainerRead(ORMModel):
    id: uuid.UUID
    node_id: uuid.UUID
    container_id: str
    name: str
    image: str
    state: str
    status_text: str | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    ports: dict[str, Any] = Field(default_factory=dict)
    # Whether the platform created it, and therefore whether control is permitted.
    managed: bool
    exit_code: int | None = None
    last_seen_at: dt.datetime | None = None


class ContainerLogsRead(BaseModel):
    container_id: str
    lines: str
    tail: int


class ContainerActionResponse(BaseModel):
    container_id: str
    action: str
    state: str | None = None
    message: str


# ---------------------------------------------------------------------------
# Self-enrolment (M04)
# ---------------------------------------------------------------------------
class NodeEnrollmentCreateRequest(BaseModel):
    """Everything an administrator supplies. Notably not a URL and not a token."""

    name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="The name the node will take. The enrolling host cannot change it.",
    )
    description: str | None = Field(default=None, max_length=1000)
    labels: dict[str, Any] = Field(default_factory=dict)
    verify_tls: bool = Field(
        default=True,
        description=(
            "Whether to verify the agent's certificate. Set here rather than by the "
            "enrolling node, which must not be able to downgrade its own transport."
        ),
    )
    ttl_seconds: int | None = Field(default=None, ge=300, le=86400)
    reenroll: bool = Field(
        default=False,
        description=(
            "Allow this to replace the credentials of a node that already exists — for a "
            "rebuilt host or a token rotation. The node keeps its id, GPUs and history."
        ),
    )


class NodeEnrollmentCreatedResponse(BaseModel):
    """The only response that ever contains the enrolment token.

    Only its hash is stored, so the platform genuinely cannot show it again. `command` is
    the whole point of the feature: the exact line to run on the GPU host.
    """

    id: uuid.UUID
    node_name: str
    token_prefix: str
    enrollment_token: str
    expires_at: dt.datetime
    server_url: str
    command: str
    warning: str = (
        "Copy this now. Only a hash is stored, so the token cannot be shown again — "
        "revoke and re-issue if it is lost."
    )


class NodeEnrollmentRead(BaseModel):
    """A pending or settled enrolment. Never the token or its hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_name: str
    description: str | None
    status: str
    token_prefix: str
    verify_tls: bool
    expires_at: dt.datetime
    consumed_at: dt.datetime | None
    consumed_from_ip: str | None
    advertised_url: str | None
    resolved_ip: str | None
    attempts: int
    last_error: str | None
    node_id: uuid.UUID | None
    created_at: dt.datetime


class NodeEnrollRequest(BaseModel):
    """What the install script sends. The token is in the Authorization header."""

    agent_token: str = Field(
        min_length=32,
        max_length=512,
        description=(
            "The bearer token the script generated on the node and configured as "
            "NODE_AGENT_AUTH_TOKEN. Generated there so it never passes through the "
            "console, a download, or an administrator's clipboard."
        ),
    )
    advertised_url: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "Where the control plane should reach this agent. Omit to have it inferred "
            "from the source address of this request, which is correct only on a flat "
            "network with no NAT in between."
        ),
    )
    node_name: str | None = Field(default=None, max_length=128)
    agent_version: str | None = Field(default=None, max_length=32)


class NodeEnrollResponse(BaseModel):
    node_id: uuid.UUID
    node_name: str
    status: str
    gpus_seen: int
    message: str
