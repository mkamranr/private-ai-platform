"""Budget alerts (E12): token budgets on quotas, and monthly cost budgets.

Each test hides quotas, budgets and alerts other processes committed (the deletes roll
back with the test), and names its subjects uniquely, so it only sees its own usage.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.chargeback import CostBudget, CostRate
from app.models.models_registry import UsageRecord
from app.models.operations import Alert, AlertSeverity, GatewayQuota
from app.models.teams import Team, TeamMembership
from app.repositories.audit import AuditRepository
from app.repositories.chargeback import (
    ChargebackRepository,
    CostBudgetRepository,
    CostRateRepository,
)
from app.repositories.models_registry import ApiKeyRepository
from app.repositories.operations import AlertRepository, GatewayQuotaRepository
from app.repositories.teams import TeamRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.budgets import BudgetWatcher, CostBudgetService, level
from app.services.chargeback import ChargebackService
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/chargeback/budgets"
NOW = dt.datetime.now(dt.UTC)


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def clean(session: AsyncSession) -> None:
    for table in (GatewayQuota, CostBudget, Alert):
        await session.execute(delete(table))
    await session.flush()


async def _usage(
    session: AsyncSession,
    *,
    tokens: int,
    model: str = "budget-model",
    end_user: str | None = None,
    team: Team | None = None,
) -> None:
    session.add(
        UsageRecord(
            recorded_at=NOW,
            endpoint="chat.completions",
            requested_model=model,
            model=model,
            prompt_tokens=tokens,
            completion_tokens=0,
            end_user=end_user,
            end_user_trusted=end_user is not None,
            team_id=team.id if team else None,
        )
    )
    await session.flush()


async def _team(session: AsyncSession) -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"Budget {suffix}", slug=f"budget-{suffix}")
    session.add(team)
    await session.flush()
    return team


def _watcher(session: AsyncSession, settings, *, warn: int = 80) -> BudgetWatcher:
    audit = AuditService(AuditRepository(session))
    alert_repo = AlertRepository(session)
    keys, teams = ApiKeyRepository(session), TeamRepository(session)
    budgets = CostBudgetRepository(session)
    costs = CostBudgetService(
        budgets,
        ChargebackService(
            CostRateRepository(session), ChargebackRepository(session), audit, currency="USD"
        ),
        keys,
        teams,
        audit,
    )
    return BudgetWatcher(
        GatewayQuotaRepository(session),
        budgets,
        costs,
        AlertService(alert_repo, audit),
        alert_repo,
        keys,
        teams,
        warn_percent=warn,
    )


async def _open(session: AsyncSession, source: str) -> dict[str, Alert]:
    rows = (
        await session.scalars(
            select(Alert)
            .where(Alert.source == source, Alert.state != "RESOLVED")
            # The watcher writes with a Core upsert; reload rather than trust the cache.
            .execution_options(populate_existing=True)
        )
    ).all()
    return {a.fingerprint: a for a in rows}


def test_levels() -> None:
    assert level(Decimal(79), Decimal(100), 80) is None
    assert level(Decimal(80), Decimal(100), 80) == AlertSeverity.WARNING
    assert level(Decimal(100), Decimal(100), 80) == AlertSeverity.CRITICAL
    assert level(Decimal(5), Decimal(0), 80) is None


class TestTokenBudgets:
    async def test_warning_then_critical_then_cleared(
        self, session: AsyncSession, settings, clean
    ) -> None:
        user = _name("alice")
        quota = GatewayQuota(scope="end_user", subject=user, daily_tokens=1000)
        session.add(quota)
        await _usage(session, tokens=850, end_user=user)

        await _watcher(session, settings).run()
        (alert,) = (await _open(session, "quota_budget")).values()
        assert alert.severity == "WARNING"
        assert alert.fingerprint == f"quota_budget:{quota.id}:daily:{user}"
        assert "85%" in alert.title and "850 of 1,000" in alert.message

        await _usage(session, tokens=200, end_user=user)
        await _watcher(session, settings).run()
        (alert,) = (await _open(session, "quota_budget")).values()
        assert alert.severity == "CRITICAL" and "used up" in alert.title

        # Raising the limit is the condition going away: the alert closes itself.
        quota.daily_tokens = 100_000
        await session.flush()
        result = await _watcher(session, settings).run()
        assert result.cleared == [alert.fingerprint]
        assert await _open(session, "quota_budget") == {}

    async def test_a_wildcard_quota_watches_each_subject_except_those_with_their_own(
        self, session: AsyncSession, settings, clean
    ) -> None:
        heavy, own, light = _name("heavy"), _name("own"), _name("light")
        wildcard = GatewayQuota(scope="end_user", subject="*", monthly_tokens=1000)
        session.add_all(
            [wildcard, GatewayQuota(scope="end_user", subject=own, monthly_tokens=10**9)]
        )
        await _usage(session, tokens=900, end_user=heavy)
        await _usage(session, tokens=5000, end_user=own)  # governed by its own quota
        await _usage(session, tokens=10, end_user=light)

        await _watcher(session, settings).run()
        opened = set(await _open(session, "quota_budget"))
        prefix = f"quota_budget:{wildcard.id}:monthly:"
        assert f"{prefix}{heavy}" in opened
        assert f"{prefix}{own}" not in opened and f"{prefix}{light}" not in opened

    async def test_the_warning_threshold_is_configurable(
        self, session: AsyncSession, settings, clean
    ) -> None:
        user = _name("bob")
        session.add(GatewayQuota(scope="end_user", subject=user, daily_tokens=1000))
        await _usage(session, tokens=600, end_user=user)
        await _watcher(session, settings, warn=80).run()
        assert await _open(session, "quota_budget") == {}
        await _watcher(session, settings, warn=50).run()
        assert len(await _open(session, "quota_budget")) == 1

    async def test_disabled_quotas_are_not_watched(
        self, session: AsyncSession, settings, clean
    ) -> None:
        user = _name("carol")
        session.add(GatewayQuota(scope="end_user", subject=user, daily_tokens=10, enabled=False))
        await _usage(session, tokens=50, end_user=user)
        await _watcher(session, settings).run()
        assert await _open(session, "quota_budget") == {}


class TestCostBudgetWatch:
    async def test_team_budget_alerts_on_estimated_spend(
        self, session: AsyncSession, settings, clean
    ) -> None:
        model, team = _name("cost-model"), await _team(session)
        session.add(
            CostRate(
                model=model,
                effective_from=NOW - dt.timedelta(days=40),
                prompt_per_million=Decimal(1000),
                completion_per_million=Decimal(0),
            )
        )
        budget = CostBudget(scope="team", subject=str(team.id), monthly_amount=Decimal(10))
        session.add(budget)
        await _usage(session, tokens=9_000, model=model, team=team)  # 9.00 of 10.00

        await _watcher(session, settings).run()
        (alert,) = (await _open(session, "cost_budget")).values()
        assert alert.severity == "WARNING" and "90%" in alert.title
        assert "9.00 of 10.00 USD" in alert.message and "never block" in alert.message

        await _usage(session, tokens=2_000, model=model, team=team)
        await _watcher(session, settings).run()
        (alert,) = (await _open(session, "cost_budget")).values()
        assert alert.severity == "CRITICAL"

        budget.enabled = False
        await session.flush()
        await _watcher(session, settings).run()
        assert await _open(session, "cost_budget") == {}

    async def test_unpriced_usage_raises_nothing(
        self, session: AsyncSession, settings, clean
    ) -> None:
        team = await _team(session)
        session.add(CostBudget(scope="team", subject=str(team.id), monthly_amount=Decimal(1)))
        await _usage(session, tokens=10**6, model=_name("no-rate"), team=team)
        await _watcher(session, settings).run()
        assert await _open(session, "cost_budget") == {}


class TestCostBudgetApi:
    @pytest.fixture
    async def finance(self, session: AsyncSession, settings) -> User:
        return await _user_with(
            session, settings, [Perm.COST_VIEW, Perm.COST_MANAGE, Perm.SCOPE_ALL], name="finance"
        )

    async def test_create_list_update_delete_audited(
        self, client: AsyncClient, tokens, finance: User, session: AsyncSession, clean
    ) -> None:
        team = await _team(session)
        headers = auth_header(tokens, finance)
        created = await client.post(
            URL,
            headers=headers,
            json={"scope": "team", "subject": str(team.id), "monthly_amount": "250.00"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["subject_label"] == f"team {team.name}"
        assert body["spend"] == "0" and body["status"] == "ok" and body["warn_percent"] == 80

        listed = await client.get(URL, headers=headers)
        assert [i["id"] for i in listed.json()["items"]] == [body["id"]]

        changed = await client.patch(
            f"{URL}/{body['id']}", headers=headers, json={"enabled": False}
        )
        assert changed.json()["status"] == "disabled"
        assert (await client.delete(f"{URL}/{body['id']}", headers=headers)).status_code == 204

        actions = (
            await session.scalars(select(AuditLog.action).where(AuditLog.resource_id == body["id"]))
        ).all()
        assert sorted(actions) == [
            "COST_BUDGET_CREATED",
            "COST_BUDGET_DELETED",
            "COST_BUDGET_UPDATED",
        ]

    async def test_global_budget_and_validation(
        self, client: AsyncClient, tokens, finance: User, clean
    ) -> None:
        headers = auth_header(tokens, finance)
        first = await client.post(
            URL, headers=headers, json={"scope": "global", "monthly_amount": "1000"}
        )
        assert first.status_code == 201 and first.json()["subject"] == "*"
        again = await client.post(
            URL, headers=headers, json={"scope": "global", "monthly_amount": "5"}
        )
        assert again.status_code == 409
        missing = await client.post(
            URL,
            headers=headers,
            json={"scope": "team", "subject": str(uuid.uuid4()), "monthly_amount": "5"},
        )
        assert missing.status_code == 422
        zero = await client.post(
            URL, headers=headers, json={"scope": "global", "monthly_amount": "0"}
        )
        assert zero.status_code == 422

    async def test_without_scope_all_only_own_team_budgets_are_visible(
        self, client: AsyncClient, tokens, finance: User, session: AsyncSession, settings, clean
    ) -> None:
        mine, other = await _team(session), await _team(session)
        member = await _user_with(session, settings, [Perm.COST_VIEW], name="teamcost")
        session.add(TeamMembership(team_id=mine.id, user_id=member.id, role="member"))
        await session.flush()
        for payload in (
            {"scope": "global", "monthly_amount": "1000"},
            {"scope": "team", "subject": str(mine.id), "monthly_amount": "10"},
            {"scope": "team", "subject": str(other.id), "monthly_amount": "10"},
        ):
            r = await client.post(URL, headers=auth_header(tokens, finance), json=payload)
            assert r.status_code == 201, r.text

        seen = (await client.get(URL, headers=auth_header(tokens, member))).json()["items"]
        assert [i["subject"] for i in seen] == [str(mine.id)]

    async def test_viewing_needs_cost_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        nobody = await _user_with(session, settings, [], name="nocost")
        r = await client.get(URL, headers=auth_header(tokens, nobody))
        assert r.status_code == 403
