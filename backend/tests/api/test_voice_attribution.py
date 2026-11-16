"""Voice speech calls are attributed to the person and the agent's team (E27).

Before E27 the voice assistant's speech-to-text and text-to-speech calls were recorded
with an empty gateway context: no user, no team. They counted toward model budgets but
appeared under nobody on the Usage page, in chargeback or against a team budget.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.voice_ws import _context
from app.core.errors import RateLimitError
from app.models.auth import User
from app.models.operations import GatewayQuota, QuotaScope
from app.models.teams import Team
from app.models.voice import VoiceSession
from app.services.gateway import GatewayContext
from tests.api.test_internal_usage import cleanup_rows  # noqa: F401
from tests.api.test_platform_quota_enforcement import _gateway, _spent
from tests.api.test_voice import speaker, voice_agent  # noqa: F401


class TestContext:
    async def test_a_voice_call_names_the_person_and_the_agents_team(
        self, session: AsyncSession, speaker: User, voice_agent
    ) -> None:
        team = Team(name=f"Voice {uuid.uuid4().hex[:8]}", slug=f"voice-{uuid.uuid4().hex[:8]}")
        session.add(team)
        await session.flush()
        voice_agent.team_id = team.id
        voice = VoiceSession(user_id=speaker.id, agent_id=voice_agent.id)
        session.add(voice)
        await session.flush()

        context = await _context(session, voice)
        assert context.user_id == speaker.id
        assert context.team_id == team.id
        assert context.api_key is None

    async def test_a_shared_agent_has_no_team(
        self, session: AsyncSession, speaker: User, voice_agent
    ) -> None:
        voice = VoiceSession(user_id=speaker.id, agent_id=voice_agent.id)
        session.add(voice)
        await session.flush()
        context = await _context(session, voice)
        assert context.user_id == speaker.id and context.team_id is None


@pytest.fixture
async def spent_team(app, database, settings, cleanup_rows: list[str]):
    """A committed team whose 1-token daily budget is used up, and the model it used."""
    suffix = uuid.uuid4().hex[:8]
    model = f"e27-model-{suffix}"
    async with database.sessionmaker() as s:
        row = Team(name=f"E27 team {suffix}", slug=f"e27-{suffix}")
        s.add(row)
        await s.commit()
        team = row.id
    cleanup_rows.extend([model, str(team)])
    async with database.sessionmaker() as s:
        s.add(GatewayQuota(scope="team", subject=str(team), daily_tokens=1))
        await s.commit()
        await _spent(_gateway(app, database, s, settings, enforce=False), model, team)
    yield team, model
    async with database.sessionmaker() as s:
        await s.execute(delete(Team).where(Team.id == team))
        await s.commit()


class TestTeamBudget:
    async def test_charged_but_not_refused_by_default(
        self, app, database, settings, spent_team
    ) -> None:
        team, model = spent_team
        async with database.sessionmaker() as s:
            gateway = _gateway(app, database, s, settings, enforce=False)
            context = GatewayContext(owner_team_id=team)
            await gateway._admit_model(context, model)  # not refused
        assert [c.scope for c in context.admission.charges] == [QuotaScope.TEAM]

    async def test_refused_when_platform_budgets_are_enforced(
        self, app, database, settings, spent_team
    ) -> None:
        team, model = spent_team
        async with database.sessionmaker() as s:
            gateway = _gateway(app, database, s, settings, enforce=True)
            with pytest.raises(RateLimitError):
                await gateway._admit_model(GatewayContext(owner_team_id=team), model)
