"""Audit log queries (M24, §8).

**Read-only, and deliberately so.** There is no endpoint here that edits or deletes an
audit row, and there will not be one: a log an administrator can rewrite answers no
question anyone would ask it. Retention is a database-level operation an operator performs
deliberately (M25), not an API call.

The platform has been writing these records since Phase 0. This is the first thing that
reads them, which is the point of the AUDITOR role existing at all — §M03 gives it
``audit.view`` and almost nothing else, so a reviewer can see what happened without being
able to change it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import AuditDep, AuditRepositoryDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.audit import AuditLogRead
from app.schemas.common import Page
from app.services.audit_export import AuditExportService, ExportFormat

router = APIRouter(prefix="/audit", tags=["audit"], route_class=CommittingRoute)


async def audit_filters(
    user_id: uuid.UUID | None = Query(default=None, description="Who did it."),
    action: str | None = Query(default=None, description="Exact action name, e.g. USER_LOGIN."),
    resource_type: str | None = Query(default=None, description="e.g. 'user', 'model', 'tool'."),
    resource_id: str | None = Query(default=None, description="What it was done to."),
    result: str | None = Query(default=None, pattern="^(SUCCESS|FAILURE|DENIED)$"),
    since: dt.datetime | None = Query(default=None),
    until: dt.datetime | None = Query(default=None),
    request_id: str | None = Query(
        default=None,
        max_length=64,
        description="Correlation id: every record one request wrote, matching its log lines.",
    ),
) -> dict[str, Any]:
    """The filters shared by search and export, so a download holds what the page showed."""
    return {
        "user_id": user_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result": result,
        "since": since,
        "until": until,
        "request_id": request_id.strip() if request_id else None,
    }


AuditFilters = Annotated[dict[str, Any], Depends(audit_filters)]


@router.get("", response_model=Page[AuditLogRead], summary="Search the audit log")
async def search_audit(
    repository: AuditRepositoryDep,
    _actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
    filters: AuditFilters,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditLogRead]:
    """Newest first, with a real total.

    The total matters more here than on other lists: without it a reviewer cannot tell
    "this is everything that matched" from "the page limit truncated it", and on an audit
    log those are very different conclusions to draw.
    """
    rows = await repository.search(**filters, limit=limit, offset=offset)
    total = await repository.count(**filters)
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/export",
    summary="Download the audit log as CSV or JSON Lines",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/x-ndjson": {}}}},
)
async def export_audit(
    repository: AuditRepositoryDep,
    audit: AuditDep,
    actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
    filters: AuditFilters,
    export_format: Annotated[ExportFormat, Query(alias="format")] = "csv",
) -> Response:
    """Every row the filters match, newest first, up to 100,000 (E54).

    A POST because it is audited: the audit trail is leaving the platform. When the cap
    cut rows off, ``X-Export-Truncated: true`` says so; narrow the date range for the rest.
    """
    service = AuditExportService(repository, audit)
    result = await service.export(actor=actor, export_format=export_format, filters=filters)
    await service.commit()
    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"',
        "Cache-Control": "no-store",
        "X-Export-Rows": str(result.rows),
    }
    if result.truncated:
        headers["X-Export-Truncated"] = "true"
    return Response(content=result.body, media_type=result.media_type, headers=headers)


@router.get("/actions", response_model=list[str], summary="Actions present in the log")
async def list_actions(
    repository: AuditRepositoryDep,
    _actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
) -> list[str]:
    """What is actually recorded, not what could be.

    Drawn from the data rather than the ``AuditAction`` enum: offering a reviewer forty
    filter options that all return nothing is worse than offering the eight that will not.
    """
    return list(await repository.distinct_actions())
