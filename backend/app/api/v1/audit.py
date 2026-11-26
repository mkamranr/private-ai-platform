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
from fastapi.responses import StreamingResponse

from app.api.deps import AuditDep, AuditRepositoryDep, SettingsDep, require_permission
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
    """Every row the filters match, newest first (E54), streamed with no cap (E100).

    A POST because it is audited: the audit trail is leaving the platform.
    ``X-Export-Rows`` is the count, fixed when the export starts: rows recorded after
    that are left out.
    """
    service = AuditExportService(repository, audit)
    export = await service.start(actor=actor, export_format=export_format, filters=filters)
    await service.commit()
    headers = {
        "Content-Disposition": f'attachment; filename="{export.filename}"',
        "Cache-Control": "no-store",
        "X-Export-Rows": str(export.rows),
    }
    return StreamingResponse(
        service.stream(export, export_format), media_type=export.media_type, headers=headers
    )


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


@router.get("/push", summary="The audit push to a SIEM: where and how far")
async def audit_push_status(
    repository: AuditRepositoryDep,
    settings: SettingsDep,
    _actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
) -> dict[str, Any]:
    """E100: whether ``AUDIT_PUSH__URL`` is set, the collector's address (host only), and
    the push's progress: the time of the last row sent, totals and the last error."""
    from app.models.system import AuditPushState
    from app.services.audit_push import display_url

    push = settings.audit_push
    state = await repository.session.get(AuditPushState, 1)
    return {
        "configured": bool(push.url),
        "url": display_url(push.url),
        "format": push.format,
        "interval_seconds": push.interval_seconds,
        "cursor_at": state.cursor_at if state else None,
        "pushed_total": state.pushed_total if state else 0,
        "last_success_at": state.last_success_at if state else None,
        "last_failure_at": state.last_failure_at if state else None,
        "last_error": state.last_error if state else None,
        "consecutive_failures": state.consecutive_failures if state else 0,
    }
