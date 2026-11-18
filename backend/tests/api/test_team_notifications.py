"""Per-team notification routing (E46): alerts carry their team; a team's channel
announces only that team's alerts, a platform channel every alert."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.chargeback import CostBudgetScope
from app.models.operations import Alert, QuotaScope
from app.models.teams import Team
from app.repositories.teams import TeamRepository
from app.services.budgets import BudgetWatcher
from tests.api.conftest import _user_with
from tests.api.test_approval_reminders import REMIND, TTL, _reminders
from tests.api.test_approver_reminders import _team_approval
from tests.api.test_notifications import (  # noqa: F401 - fixtures
    Hook,
    _channel,
    _deliveries,
    _raise,
    _run,
    clean,
    hook,
)
from tests.conftest import auth_header

URL = "/api/v1/notifications/channels"


async def _team(session: AsyncSession, label: str = "T") -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"E46 {label} {suffix}", slug=f"e46-{label.lower()}-{suffix}")
    session.add(team)
    await session.flush()
    return team


async def _alert(session: AsyncSession, fingerprint: str) -> Alert:
    return (
        await session.execute(
            select(Alert)
            .where(Alert.fingerprint == fingerprint)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


class TestRouting:
    async def test_a_team_channel_hears_only_its_team(
        self, session: AsyncSession, settings: Settings, hook: Hook, clean
    ) -> None:
        mine, theirs = await _team(session, "A"), await _team(session, "B")
        team_channel = await _channel(session, settings, name="team-a", team_id=mine.id)
        platform = await _channel(session, settings, name="platform")
        await _raise(session, fingerprint="a:1", title="Team A thing", team_id=mine.id)
        await _raise(session, fingerprint="b:1", title="Team B thing", team_id=theirs.id)
        await _raise(session, fingerprint="p:1", title="Platform thing")

        result = await _run(session, settings, hook)
        assert result.sent == 4
        by_channel: dict[uuid.UUID, set[str]] = {}
        for delivery in await _deliveries(session):
            by_channel.setdefault(delivery.channel_id, set()).add(delivery.title)
        assert by_channel[team_channel.id] == {"Team A thing"}
        assert by_channel[platform.id] == {"Team A thing", "Team B thing", "Platform thing"}

    async def test_a_raised_again_alert_follows_its_resource_to_a_new_team(
        self, session: AsyncSession, clean
    ) -> None:
        old, new = await _team(session, "Old"), await _team(session, "New")
        await _raise(session, fingerprint="moved:1", team_id=old.id)
        await _raise(session, fingerprint="moved:1", team_id=new.id)
        alert = await _alert(session, "moved:1")
        assert alert.team_id == new.id and alert.occurrences == 2


class TestWhichAlertsCarryATeam:
    async def test_an_approval_reminder_carries_its_agents_team(
        self, session: AsyncSession, superuser: User
    ) -> None:
        team = await _team(session)
        _, call = await _team_approval(session, superuser, team)
        await _reminders(session).run(ttl_hours=TTL, reminder_hours=REMIND)
        alert = await _alert(session, f"approval:tool:{call.id}")
        assert alert.team_id == team.id

    async def test_team_budgets_carry_their_team(self, session: AsyncSession) -> None:
        team = await _team(session)
        watcher: Any = SimpleNamespace(_teams=TeamRepository(session))
        team_of = BudgetWatcher._team_of
        assert await team_of(watcher, QuotaScope.TEAM, str(team.id)) == team.id
        assert await team_of(watcher, CostBudgetScope.TEAM, str(team.id)) == team.id
        # Other scopes, a team deleted since, and a malformed subject: platform-wide.
        assert await team_of(watcher, QuotaScope.API_KEY, str(team.id)) is None
        assert await team_of(watcher, QuotaScope.TEAM, str(uuid.uuid4())) is None
        assert await team_of(watcher, QuotaScope.TEAM, "not-a-uuid") is None


class TestApi:
    @pytest.fixture
    async def admin(self, session: AsyncSession, settings: Settings) -> User:
        return await _user_with(session, settings, [Perm.NOTIFICATION_MANAGE], name="e46")

    async def test_set_shown_audited_changed_and_cleared(
        self, client: AsyncClient, tokens, session: AsyncSession, admin: User, clean
    ) -> None:
        headers = auth_header(tokens, admin)
        team, other = await _team(session, "A"), await _team(session, "B")
        created = await client.post(
            URL,
            json={
                "name": "team-a-chat",
                "kind": "webhook",
                "url": "http://hooks.internal/a",
                "team_id": str(team.id),
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["team_id"] == str(team.id)
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.resource_id == body["id"],
                    AuditLog.action == "NOTIFICATION_CHANNEL_CREATED",
                )
            )
        ).scalar_one()
        assert audit.meta["team_id"] == str(team.id)

        # Moving it to another team starts it afresh, so it does not replay that team's
        # open alerts.
        before = dt.datetime.fromisoformat(body["active_since"])
        moved = await client.patch(
            f"{URL}/{body['id']}", json={"team_id": str(other.id)}, headers=headers
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["team_id"] == str(other.id)
        assert dt.datetime.fromisoformat(moved.json()["active_since"]) >= before

        cleared = await client.patch(f"{URL}/{body['id']}", json={"team_id": None}, headers=headers)
        assert cleared.status_code == 200 and cleared.json()["team_id"] is None

        # A change that leaves the team out keeps it.
        await client.patch(f"{URL}/{body['id']}", json={"team_id": str(team.id)}, headers=headers)
        kept = await client.patch(
            f"{URL}/{body['id']}", json={"min_severity": "CRITICAL"}, headers=headers
        )
        assert kept.json()["team_id"] == str(team.id)

    async def test_an_unknown_team_is_refused(
        self, client: AsyncClient, tokens, admin: User, clean
    ) -> None:
        response = await client.post(
            URL,
            json={
                "name": "nowhere",
                "kind": "webhook",
                "url": "http://hooks.internal/x",
                "team_id": str(uuid.uuid4()),
            },
            headers=auth_header(tokens, admin),
        )
        assert response.status_code == 422, response.text

    async def test_a_team_with_a_channel_cannot_be_deleted(
        self,
        client: AsyncClient,
        tokens,
        session: AsyncSession,
        settings: Settings,
        superuser: User,
        clean,
    ) -> None:
        team = await _team(session)
        channel = await _channel(session, settings, name="team-chat", team_id=team.id)
        headers = auth_header(tokens, superuser)
        refused = await client.delete(f"/api/v1/teams/{team.id}", headers=headers)
        assert refused.status_code == 409, refused.text
        assert "notification_channels" in refused.text

        await session.delete(channel)
        await session.flush()
        deleted = await client.delete(f"/api/v1/teams/{team.id}", headers=headers)
        assert deleted.status_code in (200, 204), deleted.text
