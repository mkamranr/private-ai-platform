"""Request and response bodies for operations and diagnostics (E7)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Backup, bundle and rollback point names are timestamps (``20261111T101500Z``) in
#: practice. Anything outside this pattern is refused before it reaches a shell.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$"


# -- runner inventory (what the host runner reports) ---------------------------
class InventoryBackup(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(pattern=NAME_PATTERN)
    created_at: str | None = None
    platform_version: str | None = None
    schema_revision: str | None = None
    size_bytes: int | None = None
    artifacts: list[str] = Field(default_factory=list)
    #: Whether the backup's encryption-key fingerprint matches this install's key.
    key_matches: bool | None = None
    complete: bool = True


class InventoryBundle(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(pattern=NAME_PATTERN)
    created_at: str | None = None
    platform_version: str | None = None
    format_version: int | None = None
    size_bytes: int | None = None
    #: valid, invalid, unsigned, untrusted (signed, but no trust key configured), error
    signature: str = "unsigned"
    signature_detail: str | None = None
    signer_fingerprint: str | None = None
    has_sbom: bool = False


class InventoryRollbackPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(pattern=NAME_PATTERN)
    created_at: str | None = None
    platform_version: str | None = None


class InventoryDisk(BaseModel):
    path: str | None = None
    free_bytes: int | None = None
    total_bytes: int | None = None


class RunnerInventory(BaseModel):
    model_config = ConfigDict(extra="allow")

    root: str | None = None
    installed: dict[str, Any] | None = None
    backups: list[InventoryBackup] = Field(default_factory=list)
    bundles: list[InventoryBundle] = Field(default_factory=list)
    rollback_points: list[InventoryRollbackPoint] = Field(default_factory=list)
    disk: InventoryDisk | None = None
    trust_key: dict[str, Any] | None = None
    tools: dict[str, Any] = Field(default_factory=dict)


# -- runner protocol -------------------------------------------------------------
class RunnerHeartbeat(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    host: str | None = Field(default=None, max_length=255)
    version: str | None = Field(default=None, max_length=64)
    inventory: RunnerInventory = Field(default_factory=RunnerInventory)


class RunnerHeartbeatAck(BaseModel):
    server_time: dt.datetime
    queued: int


class RunnerClaim(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)


class RunnerJobSpec(BaseModel):
    """Everything the runner needs, and keeps, to execute and later re-report a job."""

    id: uuid.UUID
    kind: str
    params: dict[str, Any]
    requested_by: uuid.UUID | None = None
    requested_by_name: str | None = None
    reason: str | None = None
    created_at: dt.datetime | None = None


class RunnerReport(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    status: Literal["RUNNING", "SUCCEEDED", "FAILED"]
    #: New output lines since the last report. Appended, never replaced.
    log: list[str] = Field(default_factory=list, max_length=5000)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=8000)
    #: The spec as claimed. Lets the API recreate a job a restore wiped from the database.
    job: RunnerJobSpec | None = None
    started_at: dt.datetime | None = None


# -- admin API ---------------------------------------------------------------------
class OpsJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    params: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    requested_by: uuid.UUID | None
    requested_by_name: str | None
    reason: str | None
    runner: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    log_lines: int = 0


class OpsJobDetail(OpsJobRead):
    log: list[str]


class RunnerRead(BaseModel):
    name: str
    host: str | None
    version: str | None
    last_seen_at: dt.datetime
    online: bool
    inventory: RunnerInventory


class RunnerStatus(BaseModel):
    """What every Operations page shows first: can anything run right now?"""

    online: bool
    runner: RunnerRead | None
    runners: list[RunnerRead]
    active_job: OpsJobRead | None
    queued: int
    stale_after_seconds: int


class JobRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ConfirmedJobRequest(BaseModel):
    """Destructive operations: the operator types the target's name back."""

    confirm: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=3, max_length=1000)


class UpgradeRequest(ConfirmedJobRequest):
    bundle: str = Field(pattern=NAME_PATTERN)
    #: Apply database migrations after the new images start. upgrade.sh leaves them to
    #: the operator; the runner runs ``alembic upgrade head`` when this is set.
    migrate: bool = True


class RollbackRequest(ConfirmedJobRequest):
    point: str = Field(pattern=NAME_PATTERN)


class BackupItem(InventoryBackup):
    last_verify: OpsJobRead | None = None


class BackupList(BaseModel):
    runner_online: bool
    items: list[BackupItem]


class BundleItem(InventoryBundle):
    last_verify: OpsJobRead | None = None


class BundleList(BaseModel):
    runner_online: bool
    installed: dict[str, Any] | None
    trust_key: dict[str, Any] | None
    items: list[BundleItem]


class RollbackList(BaseModel):
    runner_online: bool
    items: list[InventoryRollbackPoint]


class PreflightCheck(BaseModel):
    id: str
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str


class Preflight(BaseModel):
    bundle: str
    ok: bool
    checks: list[PreflightCheck]


# -- diagnostics ----------------------------------------------------------------------
class ComponentVersion(BaseModel):
    component: str
    version: str | None
    detail: str | None = None
    status: Literal["ok", "unavailable", "unknown"] = "ok"


class VersionInventory(BaseModel):
    platform_version: str
    environment: str
    schema_revision: str | None
    schema_head: str | None
    schema_current: bool | None
    installed_bundle: dict[str, Any] | None
    components: list[ComponentVersion]
    nodes: list[dict[str, Any]]
    collected_at: dt.datetime


class RedactedConfig(BaseModel):
    config: dict[str, Any]
    redacted_fields: int
