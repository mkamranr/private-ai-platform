"""E103: gateway usage records past ``gateway.usage_retention_days`` are deleted once
their day is rolled up; chargeback says when a period reaches back past them.

The test database is shared, so these tests count rows for model names only they use.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import GatewaySettings
from app.core.scope import Scope
from app.models.models_registry import UsageDailyRollup, UsageRecord
from app.repositories.audit import AuditRepository
from app.repositories.chargeback import ChargebackRepository, CostRateRepository
from app.repositories.models_registry import UsageRepository
from app.services.audit import AuditService
from app.services.chargeback import ChargebackService
from app.services.dashboard import prune_usage, roll_up_usage
from app.services.runtime_settings import RUNTIME_SETTINGS
from tests.api.test_dashboard_range import _record


async def _records(session: AsyncSession, model: str) -> int:
    rows = await session.execute(select(UsageRecord.id).where(UsageRecord.model == model))
    return len(rows.all())


class TestPrune:
    async def test_off_by_default(self, session: AsyncSession, settings) -> None:
        assert settings.gateway.usage_retention_days == 0
        model = f"keep-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        session.add(_record(model, now - dt.timedelta(days=900)))
        await session.flush()
        repo = UsageRepository(session)
        await roll_up_usage(repo, session.flush, today=now.date())
        assert await prune_usage(repo, session.flush, retention_days=0) == 0
        assert await _records(session, model) == 1

    async def test_old_rolled_up_days_are_deleted_and_still_counted(
        self, session: AsyncSession
    ) -> None:
        model = f"prune-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        old, recent = now - dt.timedelta(days=200), now - dt.timedelta(days=150)
        for at in (old, old + dt.timedelta(minutes=1), recent):
            session.add(_record(model, at))
        await session.flush()
        repo = UsageRepository(session)
        await roll_up_usage(repo, session.flush, today=now.date())
        mine = UsageDailyRollup.model == model
        before = await repo.rollup_summary(first=old.date(), where=mine)
        assert before[0]["requests"] == 3

        deleted = await prune_usage(
            repo, session.flush, retention_days=180, today=now.date(), batch=1
        )
        assert deleted >= 2  # a record per batch here
        assert await _records(session, model) == 1  # the recent one is kept
        # The dashboard's long ranges read the rollups, which are untouched...
        assert await repo.rollup_summary(first=old.date(), where=mine) == before
        # ...and not written again from the records that are left.
        await roll_up_usage(repo, session.flush, today=now.date())
        assert await repo.rollup_summary(first=old.date(), where=mine) == before

    async def test_days_not_yet_rolled_up_are_kept(self, session: AsyncSession) -> None:
        model = f"unrolled-{uuid.uuid4().hex[:8]}"
        now = dt.datetime.now(dt.UTC)
        session.add(_record(model, now))
        await session.flush()
        repo = UsageRepository(session)
        await roll_up_usage(repo, session.flush, today=now.date())
        # Long after, by the clock, but today has never been rolled up.
        later = now.date() + dt.timedelta(days=1000)
        await prune_usage(repo, session.flush, retention_days=100, today=later)
        assert await _records(session, model) == 1


class TestChargeback:
    async def test_a_period_past_the_records_kept_is_flagged(self, session: AsyncSession) -> None:
        service = ChargebackService(
            CostRateRepository(session),
            ChargebackRepository(session),
            AuditService(AuditRepository(session)),
            currency="USD",
            usage_retention_days=120,
        )
        now = dt.datetime.now(dt.UTC)
        kept_since = now.date() - dt.timedelta(days=120)
        early = await service.report(
            group="model",
            start=kept_since - dt.timedelta(days=10),
            end=kept_since,
            scope=Scope.everything(),
            now=now,
        )
        assert early["records_kept_since"] == kept_since
        assert "kept for 120 days" in early["pruned_note"]
        later = await service.report(
            group="model", start=kept_since, end=now.date(), scope=Scope.everything(), now=now
        )
        assert later["pruned_note"] is None

    async def test_nothing_flagged_when_every_record_is_kept(self, session: AsyncSession) -> None:
        service = ChargebackService(
            CostRateRepository(session),
            ChargebackRepository(session),
            AuditService(AuditRepository(session)),
            currency="USD",
        )
        report = await service.report(
            group="model",
            start=dt.date(2020, 1, 1),
            end=dt.date(2020, 1, 31),
            scope=Scope.everything(),
        )
        assert report["records_kept_since"] is None and report["pruned_note"] is None


class TestSetting:
    def test_zero_or_at_least_a_hundred_days(self) -> None:
        assert GatewaySettings(usage_retention_days=0).usage_retention_days == 0
        assert GatewaySettings(usage_retention_days=100).usage_retention_days == 100
        with pytest.raises(PydanticValidationError):
            GatewaySettings(usage_retention_days=30)

    def test_editable_from_the_console(self) -> None:
        entry = next(s for s in RUNTIME_SETTINGS if s.key == "gateway.usage_retention_days")
        assert entry.category == "retention"
        assert entry.check is not None
        entry.check(0)
        entry.check(365)
        with pytest.raises(ValueError):
            entry.check(30)
