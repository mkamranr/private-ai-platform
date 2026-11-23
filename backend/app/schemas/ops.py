"""Request and response bodies for operations and diagnostics (E7)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Backup, bundle and rollback point names are timestamps (``20261111T101500Z``) in
#: practice. Anything outside this pattern is refused before it reaches a shell.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$"


# -- runner inventory (what the host runner reports) ---------------------------
class CatalogueBase(BaseModel):
    """E64: a knowledge base a backup holds, as recorded when it was taken."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    name: str
    display_name: str | None = None
    team_id: uuid.UUID | None = None


class CatalogueTeam(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID
    name: str


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
    #: E23: whether the runner's off-host directory holds a complete copy. None when no
    #: copy directory is configured.
    copied: bool | None = None
    #: E60: what a restore of it can bring back (``database``, ``files``, ``vectors``).
    #: None from a runner older than E60.
    parts: list[str] | None = None
    #: E64: the knowledge bases and teams it holds, for a scoped restore. ``catalogued``
    #: is False for a backup taken before E64 (restore its bases by name on the host).
    knowledge_bases: list[CatalogueBase] = Field(default_factory=list)
    teams: list[CatalogueTeam] = Field(default_factory=list)
    catalogued: bool = False


class InventoryBackupCopy(BaseModel):
    """E23: the runner's off-host copy target, configured on the host: a directory
    (``OPS__BACKUP_COPY_DIR``) or, since E31, S3-compatible storage."""

    model_config = ConfigDict(extra="allow")

    configured: bool = False
    #: ``directory`` or ``s3`` (E31). None from a runner older than E31: a directory.
    kind: str | None = None
    #: The directory, or ``s3://bucket/prefix at endpoint``. Never credentials.
    path: str | None = None
    #: The directory exists and is writable now, or the bucket answers. A mount that went
    #: away, or an endpoint that refuses the keys, is False.
    reachable: bool | None = None
    #: Free space, for a directory. None for S3, which does not say.
    free_bytes: int | None = None
    total_bytes: int | None = None
    #: Why the target cannot be used as configured (a missing S3 setting, say).
    error: str | None = None


class InventoryBundle(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(pattern=NAME_PATTERN)
    created_at: str | None = None
    platform_version: str | None = None
    format_version: int | None = None
    size_bytes: int | None = None
    #: valid, invalid, unsigned, untrusted (signed, but no trust key configured), error;
    #: E63: retired or revoked (signed by a key this site no longer accepts). Judged after
    #: the trust updates the bundle carries, which ``trust_updates`` lists.
    signature: str = "unsigned"
    signature_detail: str | None = None
    signer_fingerprint: str | None = None
    trust_updates: list[dict[str, Any]] = Field(default_factory=list)
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
    backup_copy: InventoryBackupCopy = Field(default_factory=InventoryBackupCopy)
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


RestorePart = Literal["database", "files", "vectors"]
RESTORE_PARTS: tuple[str, ...] = ("database", "files", "vectors")


class RestoreRequest(ConfirmedJobRequest):
    """E60: ``parts`` restores only some of a backup; omitted, everything it holds.

    E64: ``knowledge_base_ids`` or ``team_id`` restores only those knowledge bases (or the
    ones the team owns in the backup), leaving the rest of the platform as it is.

    E68: with ``team_id`` the team's setup comes back too (members, tools, agents,
    workflows, API clients and keys, quotas, cost budgets), unless ``knowledge_only``.
    """

    parts: list[RestorePart] | None = Field(default=None, min_length=1)
    knowledge_base_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)
    team_id: uuid.UUID | None = None
    knowledge_only: bool = False

    @model_validator(mode="after")
    def _one_scope(self) -> RestoreRequest:
        if self.knowledge_base_ids and self.team_id:
            raise ValueError("Choose knowledge bases or a team, not both.")
        if self.knowledge_only and self.team_id is None:
            raise ValueError("knowledge_only goes with team_id.")
        return self


FINGERPRINT_PATTERN = r"^sha256:[0-9a-f]{64}$"
TrustAction = Literal["retire", "revoke"]


class TrustChangeRequest(ConfirmedJobRequest):
    """E63: retire (a planned end) or revoke (compromised) a bundle signing key.

    ``confirm`` is the action typed back: ``retire`` or ``revoke``.
    """

    fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    action: TrustAction


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


class BackupScheduleStatus(BaseModel):
    """The backup schedule as it stands (E12). Settings are runtime settings."""

    enabled: bool
    interval_hours: int
    keep: int
    #: E23: days complete backups are kept; 0 = no age limit.
    keep_days: int = 0
    runner_online: bool
    #: E23: the runner's off-host copy directory and whether the newest backup is in it.
    copy_configured: bool = False
    #: E31: ``directory`` or ``s3``.
    copy_kind: str | None = None
    copy_path: str | None = None
    copy_reachable: bool | None = None
    copy_error: str | None = None
    newest_copied: bool | None = None
    newest_backup: str | None
    newest_backup_at: dt.datetime | None
    next_due_at: dt.datetime | None
    overdue: bool
    #: Complete backups the next retention run would delete.
    prune_candidates: list[str]
    last_job: dict[str, Any] | None
    note: str | None
