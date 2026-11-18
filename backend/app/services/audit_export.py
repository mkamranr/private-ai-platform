"""Audit log export (E54).

A reviewer who has to hand the audit trail to someone else (an auditor, a SIEM, an
incident ticket) previously had to page through the console a hundred rows at a time.
This builds one file of the rows a filter matches, newest first:

* **CSV** for a spreadsheet. Every text cell goes through ``core/csv_safe.py``: usernames
  that were tried at sign-in, user agents and messages are all attacker-chosen text.
* **JSON Lines** for a log pipeline, one record per line, ``metadata`` kept as an object.

The export is itself audited (``AUDIT_EXPORTED``, with the filters and row count), because
the audit trail leaving the platform is exactly the kind of event it exists to record.

Rows are capped at :data:`MAX_EXPORT_ROWS` and the caller is told when the cap was hit, so
a truncated file is never mistaken for a complete one. A narrower filter (a date range)
gets the rest.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from dataclasses import dataclass
from typing import Any, Literal

from app.core.csv_safe import safe_cell
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.repositories.audit import AuditRepository
from app.services.audit import AuditService

#: Enough for months of a busy platform's audit; small enough to build in memory.
MAX_EXPORT_ROWS = 100_000

ExportFormat = Literal["csv", "jsonl"]

CSV_COLUMNS = (
    "timestamp",
    "username",
    "user_id",
    "action",
    "result",
    "resource_type",
    "resource_id",
    "request_id",
    "source_ip",
    "user_agent",
    "message",
    "metadata",
)

_MEDIA_TYPES: dict[str, str] = {
    "csv": "text/csv; charset=utf-8",
    "jsonl": "application/x-ndjson; charset=utf-8",
}


@dataclass(frozen=True, slots=True)
class AuditExport:
    body: str
    filename: str
    media_type: str
    rows: int
    truncated: bool


def _record(row: AuditLog) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "timestamp": row.timestamp.isoformat(),
        "username": row.username,
        "user_id": str(row.user_id) if row.user_id else None,
        "action": row.action,
        "result": row.result,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "request_id": row.request_id,
        "source_ip": row.source_ip,
        "user_agent": row.user_agent,
        "message": row.message,
        "metadata": row.meta or {},
    }


def _csv(rows: list[AuditLog]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        record = _record(row)
        record["metadata"] = json.dumps(record["metadata"], default=str, sort_keys=True)
        writer.writerow([safe_cell(record[column]) for column in CSV_COLUMNS])
    return buffer.getvalue()


def _jsonl(rows: list[AuditLog]) -> str:
    return "".join(json.dumps(_record(row), default=str, ensure_ascii=False) + "\n" for row in rows)


class AuditExportService:
    def __init__(
        self,
        repository: AuditRepository,
        audit: AuditService,
        *,
        max_rows: int = MAX_EXPORT_ROWS,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._max_rows = max_rows

    async def export(
        self, *, actor: User, export_format: ExportFormat, filters: dict[str, Any]
    ) -> AuditExport:
        # One more than the cap, to know whether the cap cut anything off.
        found = list(await self._repository.search(**filters, limit=self._max_rows + 1))
        truncated = len(found) > self._max_rows
        rows = found[: self._max_rows]
        body = _csv(rows) if export_format == "csv" else _jsonl(rows)

        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
        filename = f"audit-{stamp}.{export_format}"
        await self._audit.record(
            AuditAction.AUDIT_EXPORTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="audit_log",
            metadata={
                "format": export_format,
                "rows": len(rows),
                "truncated": truncated,
                "filters": {
                    key: (value.isoformat() if isinstance(value, dt.datetime) else str(value))
                    for key, value in filters.items()
                    if value is not None and value != ""
                },
            },
        )
        return AuditExport(
            body=body,
            filename=filename,
            media_type=_MEDIA_TYPES[export_format],
            rows=len(rows),
            truncated=truncated,
        )

    async def commit(self) -> None:
        """Commit the export's own audit row before the file is sent."""
        await self._repository.session.commit()
