"""Operations jobs and the host-side runner (E7, plan §5.16).

Backup, restore, upgrade and rollback need ``pg_dump`` and the Docker CLI on the host.
The control plane has neither, by design: Rule 4 forbids installing tools at build time,
and §M04 keeps the Docker socket off the control plane. So these operations are split:

* the API records an **audited request** (``ops_jobs``), with who asked and why;
* the **runner** (``scripts/ops_runner.py``) runs on the host, claims the job, executes
  it with the same stdlib scripts an operator would run by hand, and reports output and
  outcome back.

The runner also reports what it can see that the control plane cannot: the backups on
disk, offline bundles waiting in its import directory, rollback points and free space.
That report is ``ops_runners.inventory``, and the Backup and Offline Updates pages read it.

A restore replaces this database, ``ops_jobs`` included, so the runner keeps each job's
full description and its final report **upserts** the row. The restored platform then
still shows the restore that produced it.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JobKind(enum.StrEnum):
    BACKUP_CREATE = "backup.create"
    BACKUP_VERIFY = "backup.verify"
    BACKUP_RESTORE = "backup.restore"
    BUNDLE_VERIFY = "bundle.verify"
    UPGRADE = "upgrade"
    ROLLBACK = "rollback"


class JobStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class OpsJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ops_jobs"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('backup.create','backup.verify','backup.restore','bundle.verify',"
            "'upgrade','rollback')",
            name="ops_job_kind",
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')",
            name="ops_job_status",
        ),
        Index("ix_ops_jobs_queue", "status", "created_at"),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    #: What to act on: ``{"backup": "20261111T101500Z"}``, ``{"bundle": "…"}``, …
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: The runner's structured outcome (e.g. the new backup's name, the versions moved).
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    #: Output lines, newest last, capped at ``ops.max_log_lines``.
    log: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_by_name: Mapped[str | None] = mapped_column(String(255))
    #: The operator's stated reason, kept with the job and in the audit record.
    reason: Mapped[str | None] = mapped_column(Text)
    runner: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


class OpsRunner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One host-side runner and the last inventory it reported."""

    __tablename__ = "ops_runners"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    host: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[str | None] = mapped_column(String(64))
    #: Backups, bundles, rollback points, installed bundle, disk — see the module doc.
    inventory: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
