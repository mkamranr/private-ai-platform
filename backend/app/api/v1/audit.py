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

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import (
    AuditDep,
    AuditPushConfigServiceDep,
    AuditRepositoryDep,
    ExportTicketsDep,
    require_permission,
)
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.audit import (
    AuditExportLink,
    AuditLogRead,
    AuditPushTestResult,
    AuditPushUpdate,
)
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


@router.post(
    "/export/link",
    response_model=AuditExportLink,
    summary="A one-time link to download an export from (for browsers)",
)
async def audit_export_link(
    request: Request,
    tickets: ExportTicketsDep,
    actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
    filters: AuditFilters,
    export_format: Annotated[ExportFormat, Query(alias="format")] = "csv",
) -> AuditExportLink:
    """E104: the same filters and format as ``POST /audit/export``, returned as a ticket
    good for one download within a minute. The console posts it as a form to ``url``, so
    the browser saves the file as it streams, and the ticket stays out of URLs (and so
    out of access logs). The export is audited when the ticket is used."""
    ticket = await tickets.issue(actor=actor, export_format=export_format, filters=filters)
    url = request.url_for("download_audit_export").path
    return AuditExportLink(url=url, ticket=ticket.ticket, expires_at=ticket.expires_at)


@router.post(
    "/export/download",
    name="download_audit_export",
    summary="Download an export through a one-time link",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}, "application/x-ndjson": {}}}},
)
async def download_audit_export(
    repository: AuditRepositoryDep,
    audit: AuditDep,
    tickets: ExportTicketsDep,
    ticket: Annotated[str, Form(max_length=128)] = "",
) -> Response:
    """E104: authenticated by the ticket (a form field), not a bearer token, because a
    browser submitting a form sends none. The ticket is used up here; the person it was issued to
    must still be active and hold ``audit.view``. Audited as ``AUDIT_EXPORTED``."""
    actor, export_format, filters = await tickets.redeem(ticket)
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


@router.get("/push", summary="The audit push to a SIEM: where, from which settings, how far")
async def audit_push_status(
    service: AuditPushConfigServiceDep,
    _actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
) -> dict[str, Any]:
    """E100: the collector's address (scheme, host and port) and the push's progress.
    E104: ``source`` says whether the settings come from the ``environment`` (which wins,
    and makes them read-only here), the ``console``, or the push is ``off``; ``config``
    is what the console saved, with ``has_authorization`` instead of the header."""
    return await service.status()


@router.put("/push", summary="Set the audit push to a SIEM from the console")
async def update_audit_push(
    payload: AuditPushUpdate,
    service: AuditPushConfigServiceDep,
    actor: Annotated[User, require_permission(Perm.SETTINGS_MANAGE)],
) -> dict[str, Any]:
    """E104: applies from the scheduler's next tick (within 10 seconds). 409 while
    ``AUDIT_PUSH__URL`` is set in the environment. Audited, without the authorization."""
    changes = payload.model_dump(exclude_unset=True, exclude={"restart_from_now"})
    await service.update(changes, actor=actor, restart_from_now=payload.restart_from_now)
    return await service.status()


@router.post(
    "/push/test", response_model=AuditPushTestResult, summary="Send the SIEM a test record"
)
async def test_audit_push(
    service: AuditPushConfigServiceDep,
    actor: Annotated[User, require_permission(Perm.SETTINGS_MANAGE)],
) -> AuditPushTestResult:
    """E104: one made-up record (action ``AUDIT_PUSH_TEST``), sent the way the push sends,
    with what the collector answered. It is not written to the audit log; the test is
    (``AUDIT_PUSH_TESTED``)."""
    ok, detail = await service.test(actor)
    return AuditPushTestResult(ok=ok, detail=detail)
