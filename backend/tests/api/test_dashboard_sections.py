"""Dashboard sections added in E8: alerts, backends, knowledge, agents, workflows,
dependencies, and "unavailable, not zero".

The test database is shared, so counts are compared before and after a change rather than
asserted absolutely.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.agents import Agent, AgentRun, AgentVersion, RunState, ToolExecution
from app.models.auth import User
from app.models.knowledge import Document, DocumentStatus, KnowledgeBase
from app.models.operations import Alert, AlertSeverity, AlertState
from app.models.teams import Team, TeamMembership
from app.repositories.dashboard import DashboardRepository
from app.repositories.infrastructure import GpuRepository
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/dashboard"


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _get(client: AsyncClient, tokens, user: User) -> dict[str, Any]:
    response = await client.get(URL, headers=auth_header(tokens, user))
    assert response.status_code == 200, response.text
    return response.json()


async def _team(session: AsyncSession) -> Team:
    suffix = _suffix()
    team = Team(name=f"Dash {suffix}", slug=f"dash-{suffix}")
    session.add(team)
    await session.flush()
    return team


async def _failed_document(session: AsyncSession, team: Team | None) -> None:
    suffix = _suffix()
    kb = KnowledgeBase(
        name=f"dash-{suffix}",
        display_name="Dash",
        collection=f"dash_{suffix}",
        embedding_model="enterprise-embed",
        embedding_dimensions=8,
        team_id=team.id if team else None,
    )
    session.add(kb)
    await session.flush()
    doc_id = uuid.uuid4()
    session.add(
        Document(
            id=doc_id,
            lineage_id=doc_id,
            knowledge_base_id=kb.id,
            filename="broken.pdf",
            content_type="application/pdf",
            size_bytes=10,
            sha256=uuid.uuid4().hex * 2,
            status=DocumentStatus.FAILED,
            error="parser exploded",
        )
    )
    await session.flush()


class TestNewSections:
    async def test_superuser_sees_the_new_sections(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        body = await _get(client, tokens, superuser)
        for name in ("dependencies", "backends", "alerts", "knowledge", "agents", "workflows"):
            assert name in body
            assert body[name] is not None or name in body["unavailable"], name
        assert body["dependencies"]["items"], "the live probes are listed"
        assert {"requests_per_minute", "errors", "error_rate_percent"} <= set(body["gateway"])

    async def test_sections_follow_their_permissions(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        monitor = await _user_with(session, settings, [Perm.MONITORING_VIEW], name="mon")
        body = await _get(client, tokens, monitor)
        for name in ("alerts", "backends", "knowledge", "agents", "workflows", "gateway"):
            assert body[name] is None, f"{name} leaked without its permission"
        assert body["unavailable"] == {}

    async def test_open_alerts_are_counted_by_severity(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        before = (await _get(client, tokens, superuser))["alerts"]
        session.add(
            Alert(
                fingerprint=f"dash-{_suffix()}",
                severity=AlertSeverity.CRITICAL,
                state=AlertState.ACTIVE,
                source="test",
                title="Disk almost full",
                last_seen_at=dt.datetime.now(dt.UTC),
            )
        )
        await session.flush()
        after = (await _get(client, tokens, superuser))["alerts"]
        assert after["critical"] == before["critical"] + 1
        assert after["open"] == before["open"] + 1
        assert any(a["title"] == "Disk almost full" for a in after["latest"])

    async def test_agent_failures_and_failed_tool_calls(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        before = (await _get(client, tokens, superuser))["agents"]
        agent = Agent(slug=f"dash-{_suffix()}", display_name="Dash")
        session.add(agent)
        await session.flush()
        version = AgentVersion(agent_id=agent.id, version=1, system_prompt="x", model="m")
        session.add(version)
        await session.flush()
        run = AgentRun(
            agent_id=agent.id, agent_version_id=version.id, input="hi", state=RunState.FAILED
        )
        session.add(run)
        await session.flush()
        session.add(ToolExecution(run_id=run.id, tool_name="http_get", success=False))
        await session.flush()

        after = (await _get(client, tokens, superuser))["agents"]
        assert after["failed"] == before["failed"] + 1
        assert after["runs"] == before["runs"] + 1
        assert after["tool_calls_failed"] == before["tool_calls_failed"] + 1


class TestTeamScope:
    async def test_knowledge_counts_follow_team_scope(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """A team's ingestion failures are its own business: a member sees them, a user
        outside the team does not, exactly as the knowledge base list behaves."""
        perms = [Perm.MONITORING_VIEW, Perm.KNOWLEDGE_VIEW]
        member = await _user_with(session, settings, perms, name="member")
        outsider = await _user_with(session, settings, perms, name="outsider")
        team = await _team(session)
        session.add(TeamMembership(team_id=team.id, user_id=member.id, role="member"))
        await session.flush()

        member_before = (await _get(client, tokens, member))["knowledge"]
        outsider_before = (await _get(client, tokens, outsider))["knowledge"]
        await _failed_document(session, team)

        member_after = (await _get(client, tokens, member))["knowledge"]
        outsider_after = (await _get(client, tokens, outsider))["knowledge"]
        assert member_after["failed"] == member_before["failed"] + 1
        assert member_after["failed_in_window"] == member_before["failed_in_window"] + 1
        assert member_after["bases"] == member_before["bases"] + 1
        assert outsider_after == outsider_before


class TestUnavailableIsNotZero:
    async def test_a_failing_source_is_reported_and_the_rest_survives(
        self, client: AsyncClient, tokens, superuser: User, monkeypatch
    ) -> None:
        """The failure is a real database error, so this also proves the section's
        savepoint keeps the request's transaction usable for the sections after it."""

        async def broken(self: DashboardRepository, **_: Any) -> dict[str, Any]:
            await self.session.execute(text("SELECT * FROM no_such_table_e8"))
            raise AssertionError("unreachable")

        monkeypatch.setattr(DashboardRepository, "alerts", broken)
        body = await _get(client, tokens, superuser)
        assert body["alerts"] is None
        assert "alerts" in body["unavailable"]
        assert "no_such_table" not in body["unavailable"]["alerts"], "no SQL in the reason"
        # Sections after the failure still answered.
        assert body["knowledge"] is not None
        assert body["activity"] is not None

    async def test_gpu_utilisation_is_unknown_when_nothing_reports(
        self, client: AsyncClient, tokens, superuser: User, monkeypatch
    ) -> None:
        async def silent(self: GpuRepository) -> dict[str, Any]:
            return {
                "total": 2,
                "allocated": 0,
                "avg_utilization_percent": 0.0,
                "memory_used_mib": 0,
                "memory_total_mib": 0,
                "reporting": 0,
                "collected_at": None,
            }

        monkeypatch.setattr(GpuRepository, "capacity_summary", silent)
        gpus = (await _get(client, tokens, superuser))["gpus"]
        assert gpus["total"] == 2
        assert gpus["reporting"] == 0
        assert gpus["avg_utilization_percent"] is None
        assert gpus["memory_total_mib"] is None
        assert gpus["collected_at"] is None
