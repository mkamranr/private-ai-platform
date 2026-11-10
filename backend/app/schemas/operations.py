"""Wire contracts for inference backends and alerts (E1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel

BackendTypeLiteral = Literal["vllm", "ollama", "openai_compatible"]

_NAME = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._ -]+$")


class BackendCreateRequest(BaseModel):
    name: str = _NAME
    backend_type: BackendTypeLiteral
    base_url: str = Field(
        min_length=1,
        max_length=512,
        description=(
            "Where the backend listens, e.g. http://gpu-01:8000. A trailing /v1 is removed "
            "for vLLM and OpenAI-compatible backends."
        ),
    )
    api_key: str | None = Field(
        default=None,
        max_length=4096,
        description="Stored encrypted. Never returned by the API.",
    )
    description: str | None = Field(default=None, max_length=2000)
    verify_tls: bool = True
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    enabled: bool = True
    labels: dict[str, str] = Field(default_factory=dict)


class BackendUpdateRequest(BaseModel):
    """Partial update. Only fields present in the request body change.

    ``api_key``: omit to keep the current key, send ``""`` to remove it.
    """

    name: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._ -]+$"
    )
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    api_key: str | None = Field(default=None, max_length=4096)
    description: str | None = Field(default=None, max_length=2000)
    verify_tls: bool | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    enabled: bool | None = None
    labels: dict[str, str] | None = None


class BackendRead(ORMModel):
    id: uuid.UUID
    name: str
    backend_type: str
    base_url: str
    description: str | None = None
    has_api_key: bool
    verify_tls: bool
    timeout_seconds: float
    enabled: bool
    labels: dict[str, str]
    status: str
    status_detail: str | None = None
    last_checked_at: dt.datetime | None = None
    last_latency_ms: float | None = None
    version: str | None = None
    capabilities: list[str]
    model_count: int | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class BackendDetail(BackendRead):
    linked_models: list[str] = Field(default_factory=list)


class ConnectionTestResponse(BaseModel):
    ok: bool
    checked_at: dt.datetime
    latency_ms: float | None = None
    version: str | None = None
    model_count: int | None = None
    error: str | None = None
    status: str


class DiscoveredModelRead(BaseModel):
    id: str
    owned_by: str | None = None
    #: Platform model name this would be registered as.
    platform_name: str
    #: Name of the catalogue entry already using that name, if any.
    registered: bool


class RegisterDiscoveredRequest(BaseModel):
    model_ids: list[str] | None = Field(
        default=None, description="Backend model ids to register. Omit to register all."
    )


class RegisterDiscoveredRow(BaseModel):
    model_id: str
    name: str
    status: str


class BackendDeletedResponse(BaseModel):
    message: str
    unlinked_models: list[str]


class AlertRead(ORMModel):
    id: uuid.UUID
    fingerprint: str
    severity: str
    state: str
    source: str
    resource_type: str | None = None
    resource_id: str | None = None
    title: str
    message: str | None = None
    occurrences: int
    created_at: dt.datetime
    last_seen_at: dt.datetime
    acknowledged_at: dt.datetime | None = None
    acknowledged_by: uuid.UUID | None = None
    resolved_at: dt.datetime | None = None
    resolved_by: uuid.UUID | None = None


class AlertSummary(BaseModel):
    """Open alerts by severity, for the dashboard badge."""

    INFO: int = 0
    WARNING: int = 0
    CRITICAL: int = 0
    total: int = 0
