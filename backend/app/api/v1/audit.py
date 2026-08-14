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
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AuditRepositoryDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.audit import AuditLogRead
from app.schemas.common import Page

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditLogRead], summary="Search the audit log")
async def search_audit(
    repository: AuditRepositoryDep,
    _actor: Annotated[User, require_permission(Perm.AUDIT_VIEW)],
    user_id: uuid.UUID | None = Query(default=None, description="Who did it."),
    action: str | None = Query(default=None, description="Exact action name, e.g. USER_LOGIN."),
    resource_type: str | None = Query(default=None, description="e.g. 'user', 'model', 'tool'."),
    resource_id: str | None = Query(default=None, description="What it was done to."),
    result: str | None = Query(default=None, pattern="^(SUCCESS|FAILURE|DENIED)$"),
    since: dt.datetime | None = Query(default=None),
    until: dt.datetime | None = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditLogRead]:
    """Newest first, with a real total.

    The total matters more here than on other lists: without it a reviewer cannot tell
    "this is everything that matched" from "the page limit truncated it", and on an audit
    log those are very different conclusions to draw.
    """
    filters = {
        "user_id": user_id,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "result": result,
        "since": since,
        "until": until,
    }
    rows = await repository.search(**filters, limit=limit, offset=offset)  # type: ignore[arg-type]
    total = await repository.count(**filters)  # type: ignore[arg-type]
    return Page[AuditLogRead](
        items=[AuditLogRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
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
