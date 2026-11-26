"""Audit log export (E54), streamed (E100).

A reviewer who has to hand the audit trail to someone else (an auditor, a SIEM, an
incident ticket) previously had to page through the console a hundred rows at a time.
This writes one file of the rows a filter matches, newest first:

* **CSV** for a spreadsheet. Every text cell goes through ``core/csv_safe.py``: usernames
  that were tried at sign-in, user agents and messages are all attacker-chosen text.
* **JSON Lines** for a log pipeline, one record per line, ``metadata`` kept as an object.

The export is itself audited (``AUDIT_EXPORTED``, with the filters and row count), because
the audit trail leaving the platform is exactly the kind of event it exists to record.

E100: the file is streamed a page at a time (keyset paging), so its size is bounded by
the filter, not by the server's memory, and there is no cap. Rows recorded after the
export started are left out, so the count announced up front is the count sent.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.core.csv_safe import safe_cell
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.repositories.audit import AuditRepository
from app.services.audit import AuditService

#: Rows read per query while streaming (E100).
EXPORT_PAGE_ROWS = 1000

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
    filename: str
    media_type: str
    rows: int
    #: The filters the stream applies, ``until`` fixed at the start of the export.
    filters: dict[str, Any]


def record(row: AuditLog) -> dict[str, Any]:
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


def _csv(rows: Iterable[AuditLog], *, header: bool) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    if header:
        writer.writerow(CSV_COLUMNS)
    for row in rows:
        fields = record(row)
        fields["metadata"] = json.dumps(fields["metadata"], default=str, sort_keys=True)
        writer.writerow([safe_cell(fields[column]) for column in CSV_COLUMNS])
    return buffer.getvalue()


def jsonl(rows: Iterable[AuditLog]) -> str:
    return "".join(json.dumps(record(row), default=str, ensure_ascii=False) + "\n" for row in rows)


class AuditExportService:
    def __init__(
        self,
        repository: AuditRepository,
        audit: AuditService,
        *,
        page_rows: int = EXPORT_PAGE_ROWS,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._page_rows = page_rows

    async def start(
        self, *, actor: User, export_format: ExportFormat, filters: dict[str, Any]
    ) -> AuditExport:
        """Count what the export will hold and audit it; ``stream`` then sends it."""
        started = dt.datetime.now(dt.UTC)
        until = filters.get("until")
        fixed = {**filters, "until": min(until, started) if until else started}
        rows = await self._repository.count(**fixed)
        stamp = started.strftime("%Y%m%d-%H%M%S")
        await self._audit.record(
            AuditAction.AUDIT_EXPORTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="audit_log",
            metadata={
                "format": export_format,
                "rows": rows,
                "filters": {
                    key: (value.isoformat() if isinstance(value, dt.datetime) else str(value))
                    for key, value in filters.items()
                    if value is not None and value != ""
                },
            },
        )
        return AuditExport(
            filename=f"audit-{stamp}.{export_format}",
            media_type=_MEDIA_TYPES[export_format],
            rows=rows,
            filters=fixed,
        )

    async def stream(self, export: AuditExport, export_format: ExportFormat) -> AsyncIterator[str]:
        """The file, a page of rows at a time."""
        before: tuple[dt.datetime, Any] | None = None
        first = True
        while True:
            page = list(
                await self._repository.export_page(
                    export.filters, before=before, limit=self._page_rows
                )
            )
            if export_format == "csv":
                if first or page:
                    yield _csv(page, header=first)
            elif page:
                yield jsonl(page)
            first = False
            if len(page) < self._page_rows:
                return
            before = (page[-1].timestamp, page[-1].id)

    async def commit(self) -> None:
        """Commit the export's own audit row before the file is sent."""
        await self._repository.session.commit()
