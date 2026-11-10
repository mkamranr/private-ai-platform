"""Inference backend, alert (E1) and gateway quota (E2) repositories."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, literal_column, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.models.models_registry import Model, UsageRecord
from app.models.operations import (
    OPEN_ALERT_STATES,
    QUOTA_WILDCARD,
    Alert,
    AlertState,
    GatewayQuota,
    InferenceBackend,
    QuotaScope,
)
from app.repositories.base import BaseRepository


class InferenceBackendRepository(BaseRepository[InferenceBackend]):
    model = InferenceBackend

    async def get_by_name(self, name: str) -> InferenceBackend | None:
        stmt = select(InferenceBackend).where(InferenceBackend.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_backends(
        self, *, enabled: bool | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[InferenceBackend]:
        stmt = select(InferenceBackend)
        if enabled is not None:
            stmt = stmt.where(InferenceBackend.enabled.is_(enabled))
        stmt = stmt.order_by(InferenceBackend.name).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_backends(self, *, enabled: bool | None = None) -> int:
        stmt = select(func.count()).select_from(InferenceBackend)
        if enabled is not None:
            stmt = stmt.where(InferenceBackend.enabled.is_(enabled))
        return int((await self.session.execute(stmt)).scalar_one())

    async def refresh(self, backend: InferenceBackend) -> None:
        """Reload server-generated columns (``updated_at``) after a flush.

        An UPDATE expires them, and reading an expired attribute later would attempt a
        lazy load outside the async context — so a service returning an updated row
        reloads it here, while it still can.
        """
        await self.session.refresh(backend)

    async def models_on(self, backend_id: uuid.UUID) -> Sequence[Model]:
        stmt = select(Model).where(Model.backend_id == backend_id).order_by(Model.name)
        return (await self.session.execute(stmt)).scalars().all()


@dataclass(frozen=True, slots=True)
class AlertUpsert:
    alert_id: uuid.UUID
    #: True when this call opened a new alert; False when it added an occurrence to one.
    created: bool


class AlertRepository(BaseRepository[Alert]):
    model = Alert

    async def list_alerts(
        self,
        *,
        state: str | None = None,
        open_only: bool = False,
        severity: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Alert]:
        stmt = self._filtered(select(Alert), state, open_only, severity)
        stmt = stmt.order_by(Alert.last_seen_at.desc()).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def count_alerts(
        self, *, state: str | None = None, open_only: bool = False, severity: str | None = None
    ) -> int:
        stmt = self._filtered(select(func.count()).select_from(Alert), state, open_only, severity)
        return int((await self.session.execute(stmt)).scalar_one())

    async def open_counts_by_severity(self) -> dict[str, int]:
        stmt = (
            select(Alert.severity, func.count())
            .where(Alert.state.in_([str(s) for s in OPEN_ALERT_STATES]))
            .group_by(Alert.severity)
        )
        return {str(sev): int(n) for sev, n in (await self.session.execute(stmt)).all()}

    @staticmethod
    def _filtered(stmt, state, open_only, severity):  # type: ignore[no-untyped-def]
        if state:
            stmt = stmt.where(Alert.state == state)
        elif open_only:
            stmt = stmt.where(Alert.state.in_([str(s) for s in OPEN_ALERT_STATES]))
        if severity:
            stmt = stmt.where(Alert.severity == severity)
        return stmt

    async def upsert_open(
        self,
        *,
        fingerprint: str,
        severity: str,
        source: str,
        title: str,
        message: str | None,
        resource_type: str | None,
        resource_id: str | None,
    ) -> AlertUpsert:
        """Open an alert, or count another occurrence of the open one.

        One statement against the partial unique index, so two workers seeing the same
        failure at the same moment produce one alert with two occurrences rather than
        a unique-violation in one of them. ``xmax = 0`` is PostgreSQL's tell for "this
        row was inserted, not updated" within an upsert.
        """
        stmt = insert(Alert).values(
            id=uuid.uuid4(),
            fingerprint=fingerprint,
            severity=severity,
            state=AlertState.ACTIVE,
            source=source,
            title=title,
            message=message,
            resource_type=resource_type,
            resource_id=resource_id,
            occurrences=1,
        )
        upsert: Any = stmt.on_conflict_do_update(
            index_elements=[Alert.fingerprint],
            index_where=text("state <> 'RESOLVED'"),
            set_={
                "occurrences": Alert.occurrences + 1,
                "last_seen_at": func.now(),
                # The latest explanation is the useful one: "timed out" an hour ago may
                # be "connection refused" now.
                "message": stmt.excluded.message,
                "severity": stmt.excluded.severity,
                "title": stmt.excluded.title,
                "updated_at": func.now(),
            },
        ).returning(Alert.id, literal_column("(xmax = 0)").label("created"))
        row = (await self.session.execute(upsert)).one()
        return AlertUpsert(alert_id=row.id, created=bool(row.created))

    async def resolve_open(self, fingerprint: str, *, at: dt.datetime) -> int:
        """Resolve the open alert for ``fingerprint`` because its condition cleared."""
        stmt = (
            update(Alert)
            .where(
                Alert.fingerprint == fingerprint,
                Alert.state.in_([str(s) for s in OPEN_ALERT_STATES]),
            )
            .values(state=AlertState.RESOLVED, resolved_at=at, resolved_by=None)
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class GatewayQuotaRepository(BaseRepository[GatewayQuota]):
    model = GatewayQuota

    async def list_quotas(self, *, scope: str | None = None) -> Sequence[GatewayQuota]:
        stmt = select(GatewayQuota)
        if scope is not None:
            stmt = stmt.where(GatewayQuota.scope == scope)
        stmt = stmt.order_by(GatewayQuota.scope, GatewayQuota.subject)
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_subject(self, scope: str, subject: str) -> GatewayQuota | None:
        stmt = select(GatewayQuota).where(
            GatewayQuota.scope == scope, GatewayQuota.subject == subject
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def applicable(self, subjects: Sequence[tuple[str, str]]) -> dict[str, GatewayQuota]:
        """The enabled quota governing each ``(scope, subject)``, keyed by scope.

        A row naming the subject wins over the scope's ``*`` row. One query for every
        scope at once, because this runs on the hot path of every gateway call.
        """
        if not subjects:
            return {}
        clauses = [
            (GatewayQuota.scope == scope) & GatewayQuota.subject.in_((subject, QUOTA_WILDCARD))
            for scope, subject in subjects
        ]
        stmt = select(GatewayQuota).where(GatewayQuota.enabled.is_(True), or_(*clauses))
        rows = (await self.session.execute(stmt)).scalars().all()

        chosen: dict[str, GatewayQuota] = {}
        for row in rows:
            current = chosen.get(row.scope)
            if current is None or current.subject == QUOTA_WILDCARD:
                chosen[row.scope] = row
        return chosen

    async def tokens_since(self, scope: str, subject: str, since: dt.datetime) -> int:
        """Tokens billed to a subject since ``since``, from ``usage_records``.

        The source of truth behind every budget counter: whenever a counter is missing
        (a new day, a Valkey restart, a quota created mid-month) it is seeded from here.
        Each scope is backed by an existing ``(column, recorded_at)`` index.
        """
        column: Any
        if scope == QuotaScope.API_KEY:
            try:
                key_id = uuid.UUID(subject)
            except ValueError:
                return 0
            condition = UsageRecord.api_key_id == key_id
        elif scope == QuotaScope.END_USER:
            condition = (UsageRecord.end_user == subject) & UsageRecord.end_user_trusted.is_(True)
        else:
            condition = UsageRecord.model == subject
        column = func.coalesce(
            func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0
        )
        stmt = select(column).where(condition, UsageRecord.recorded_at >= since)
        return int((await self.session.execute(stmt)).scalar_one())
