"""The audit API (E54): the correlation-id filter and the export.

Rows are planted in the test's own transaction with a unique request id or resource, so
each test sees only what it wrote.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.csv_safe import safe_cell
from app.core.permissions import Permission as Perm
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.models.auth import User
from app.repositories.audit import AuditRepository
from app.services.audit import AuditService
from app.services.audit_export import AuditExportService
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/audit"


async def _plant(
    session: AsyncSession,
    *,
    request_id: str | None = None,
    resource_id: str | None = None,
    username: str = "alice",
    message: str | None = None,
    meta: dict | None = None,
    at: dt.datetime | None = None,
) -> AuditLog:
    row = AuditLog(
        timestamp=at or dt.datetime.now(dt.UTC),
        username=username,
        action="MODEL_DEPLOYED",
        resource_type="model",
        resource_id=resource_id,
        result=AuditResult.SUCCESS,
        request_id=request_id,
        message=message,
        meta=meta or {},
    )
    session.add(row)
    await session.flush()
    return row


async def _auditor(session: AsyncSession, settings) -> User:
    return await _user_with(session, settings, [Perm.AUDIT_VIEW], name="auditor")


class TestRequestIdFilter:
    async def test_only_the_rows_one_request_wrote(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        request_id = uuid.uuid4().hex
        await _plant(session, request_id=request_id)
        await _plant(session, request_id=request_id)
        await _plant(session, request_id=uuid.uuid4().hex)

        response = await client.get(
            URL, params={"request_id": request_id}, headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 2
        assert {item["request_id"] for item in body["items"]} == {request_id}

    async def test_too_long_is_refused(self, client: AsyncClient, tokens, superuser: User) -> None:
        response = await client.get(
            URL, params={"request_id": "x" * 65}, headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 422


class TestExport:
    async def test_csv_is_formula_safe_and_audited(
        self, client: AsyncClient, tokens, settings, session: AsyncSession
    ) -> None:
        auditor = await _auditor(session, settings)
        resource = f"res-{uuid.uuid4().hex[:8]}"
        await _plant(
            session,
            resource_id=resource,
            username="=HYPERLINK(1)",
            message="+cmd",
            meta={"b": 1, "a": "x"},
            at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),  # older, whatever the clock
        )
        await _plant(session, resource_id=resource, username="bob")

        response = await client.post(
            f"{URL}/export",
            params={"format": "csv", "resource_id": resource},
            headers=auth_header(tokens, auditor),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert 'filename="audit-' in response.headers["content-disposition"]
        assert response.headers["x-export-rows"] == "2"
        assert "x-export-truncated" not in response.headers

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [r["username"] for r in rows] == ["bob", "'=HYPERLINK(1)"]  # newest first
        planted = rows[1]
        assert planted["message"] == "'+cmd"
        assert json.loads(planted["metadata"]) == {"a": "x", "b": 1}

        record = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.AUDIT_EXPORTED)
                .where(AuditLog.user_id == auditor.id)
            )
        ).scalar_one()
        assert record.meta["format"] == "csv" and record.meta["rows"] == 2
        assert record.meta["filters"] == {"resource_id": resource}

    async def test_jsonl_keeps_metadata_as_an_object(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        request_id = uuid.uuid4().hex
        await _plant(session, request_id=request_id, meta={"model": "llama"})

        response = await client.post(
            f"{URL}/export",
            params={"format": "jsonl", "request_id": request_id},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/x-ndjson")
        lines = response.text.splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["metadata"] == {"model": "llama"}
        assert record["request_id"] == request_id
        assert record["action"] == "MODEL_DEPLOYED"

    async def test_unknown_format_is_refused(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.post(
            f"{URL}/export", params={"format": "xlsx"}, headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 422

    async def test_needs_audit_view(
        self, client: AsyncClient, tokens, unprivileged_user: User
    ) -> None:
        response = await client.post(
            f"{URL}/export", headers=auth_header(tokens, unprivileged_user)
        )
        assert response.status_code == 403

    async def test_the_cap_is_reported(self, session: AsyncSession, superuser: User) -> None:
        resource = f"res-{uuid.uuid4().hex[:8]}"
        for _ in range(3):
            await _plant(session, resource_id=resource)
        repository = AuditRepository(session)
        service = AuditExportService(repository, AuditService(repository), max_rows=2)
        result = await service.export(
            actor=superuser, export_format="csv", filters={"resource_id": resource}
        )
        assert result.truncated is True and result.rows == 2
        assert len(list(csv.DictReader(io.StringIO(result.body)))) == 2


def test_safe_cell() -> None:
    assert safe_cell(None) == ""
    assert safe_cell("=1+1") == "'=1+1"
    assert safe_cell("-5") == "'-5"
    assert safe_cell("plain") == "plain"
    assert safe_cell(42) == "42"
