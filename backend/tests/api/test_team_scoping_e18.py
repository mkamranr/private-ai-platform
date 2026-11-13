"""Finishing team scoping (E18).

E3 checked what an agent or workflow refers to against the person saving it. E18 checks
it against the resource's **own team**, at save time, on a move and every time it runs,
and closes the reads that still ignored teams: traces, voice sessions, memory and
conversations, and the dashboard's model and gateway figures.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import VoiceSettings
from app.core.errors import NotFoundError
from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.models.agents import Agent, AgentRun, AgentVersion, RunState
from app.models.knowledge import MemoryEntry
from app.models.models_registry import Model
from app.models.workflows import RunStatus
from tests.api.conftest import _user_with
from tests.api.test_teams import EVERYTHING, _member, _model, _suffix, world  # noqa: F401
from tests.api.test_voice import _service as _voice_service
from tests.api.test_workflows import ALL as WORKFLOW_PERMS
from tests.api.test_workflows import builder, chain, node, published, run, wf  # noqa: F401
from tests.conftest import auth_header

EXTRA = [
    Perm.AGENT_CREATE,
    Perm.AGENT_EXECUTE,
    Perm.TRACE_VIEW,
    Perm.MONITORING_VIEW,
]


@pytest.fixture
async def member_a(session: AsyncSession, settings, world: dict[str, Any]):
    """A member of team A who may build agents and workflows."""
    user = await _user_with(session, settings, [*EVERYTHING, *EXTRA, *WORKFLOW_PERMS], name="e18a")
    await _member(session, world["a"], user)
    return user


async def _agent(session: AsyncSession, team_id, model: str, **version: Any) -> Agent:
    agent = Agent(slug=f"e18-{_suffix()}", display_name="E18", team_id=team_id, enabled=True)
    session.add(agent)
    await session.flush()
    session.add(
        AgentVersion(agent_id=agent.id, version=1, system_prompt="p", model=model, **version)
    )
    await session.flush()
    return agent


def _agent_body(model: str, **extra: Any) -> dict[str, Any]:
    return {
        "slug": f"e18-{_suffix()}",
        "display_name": "E18",
        "system_prompt": "You help.",
        "model": model,
        **extra,
    }


# ---------------------------------------------------------------------------
# What an agent or workflow may use
# ---------------------------------------------------------------------------
class TestReferences:
    async def test_a_shared_agent_cannot_use_a_team_model(
        self, client: AsyncClient, tokens, member_a, world: dict[str, Any]
    ) -> None:
        """Alice can see team A's model, but a shared agent would offer it to everyone."""
        headers = auth_header(tokens, member_a)
        team_model = world["models"]["a"].name

        refused = await client.post("/api/v1/agents", headers=headers, json=_agent_body(team_model))
        assert refused.status_code == 422, refused.text
        assert "belongs to another team" in refused.json()["error"]["message"]
        assert refused.json()["error"]["details"]["uses"] == [f"model {team_model!r}"]

        allowed = await client.post(
            "/api/v1/agents",
            headers=headers,
            json=_agent_body(team_model, team_id=str(world["a"].id)),
        )
        assert allowed.status_code == 201, allowed.text

    async def test_a_team_agent_may_use_shared_and_its_own(
        self, client: AsyncClient, tokens, member_a, world: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, member_a),
            json=_agent_body(
                world["aliases"]["shared"].alias,
                team_id=str(world["a"].id),
                knowledge_base_ids=[str(world["bases"]["a"].id), str(world["bases"]["shared"].id)],
            ),
        )
        assert response.status_code == 201, response.text

    async def test_an_agent_is_rechecked_when_it_runs(
        self, client: AsyncClient, tokens, superuser, session: AsyncSession, world
    ) -> None:
        """The model moved to team B after the agent was saved: the run is refused, and
        no run record is created for it."""
        model: Model = world["models"]["shared"]
        agent = await _agent(session, None, model.name)
        model.team_id = world["b"].id
        await session.flush()

        response = await client.post(
            f"/api/v1/agents/{agent.id}/execute",
            headers=auth_header(tokens, superuser),
            json={"message": "hi"},
        )
        assert response.status_code == 409, response.text
        assert "belongs to another team" in response.json()["error"]["message"]
        assert "shared resources" in response.json()["error"]["message"]

    async def test_moving_an_agent_away_from_its_model_is_refused(
        self, client: AsyncClient, tokens, member_a, session: AsyncSession, world
    ) -> None:
        agent = await _agent(
            session,
            world["a"].id,
            world["models"]["a"].name,
            knowledge_base_ids=[str(world["bases"]["a"].id)],
        )
        response = await client.put(
            "/api/v1/scope",
            headers=auth_header(tokens, member_a),
            json={"resource_type": "agent", "resource_id": str(agent.id), "team_id": None},
        )
        assert response.status_code == 409, response.text
        uses = response.json()["error"]["details"]["uses"]
        assert f"model {world['models']['a'].name!r}" in uses
        assert f"knowledge base {world['bases']['a'].id}" in uses
        await session.refresh(agent)
        assert agent.team_id == world["a"].id

    async def test_a_shared_workflow_cannot_use_a_team_model(
        self, client: AsyncClient, tokens, member_a, world: dict[str, Any]
    ) -> None:
        definition = chain(
            node("start", "start"),
            node("answer", "llm", model=world["models"]["a"].name, prompt="hi"),
            node("end", "end"),
        )
        response = await client.post(
            "/api/v1/workflows",
            headers=auth_header(tokens, member_a),
            json={"name": f"wf-{_suffix()}", "display_name": "E18", "draft": definition},
        )
        assert response.status_code == 422, response.text
        assert "belongs to another team" in response.json()["error"]["message"]

    async def test_a_workflow_step_is_rechecked_when_it_runs(
        self, client: AsyncClient, tokens, builder, wf, session: AsyncSession, world
    ) -> None:
        model: Model = world["models"]["shared"]
        definition = chain(
            node("start", "start"),
            node("answer", "llm", model=model.name, prompt="hi"),
            node("end", "end"),
        )
        workflow = await published(client, tokens, builder, definition)
        model.team_id = world["b"].id
        await session.flush()

        result = await run(client, tokens, builder, workflow["id"], {})
        assert result["status"] == RunStatus.FAILED, result
        assert "belongs to another team" in result["error"]
        assert wf.models == [], "the model was never called"


# ---------------------------------------------------------------------------
# Reads that ignored teams before E18
# ---------------------------------------------------------------------------
class TestReads:
    async def test_another_teams_trace_is_not_found(
        self, client: AsyncClient, tokens, member_a, superuser, session: AsyncSession, world
    ) -> None:
        agent = await _agent(session, world["b"].id, "enterprise-chat")
        trace_id = _suffix() * 4
        session.add(
            AgentRun(
                agent_id=agent.id,
                agent_version_id=await _version_id(session, agent),
                trace_id=trace_id,
                state=RunState.COMPLETED,
                input="x",
                user_permissions=[],
            )
        )
        await session.flush()

        hidden = await client.get(
            f"/api/v1/traces/{trace_id}", headers=auth_header(tokens, member_a)
        )
        assert hidden.status_code == 404
        seen = await client.get(
            f"/api/v1/traces/{trace_id}", headers=auth_header(tokens, superuser)
        )
        assert seen.status_code == 200, seen.text

    async def test_voice_cannot_open_another_teams_agent(
        self, session: AsyncSession, member_a, world: dict[str, Any]
    ) -> None:
        agent = await _agent(session, world["b"].id, "enterprise-chat")
        service = _voice_service(session, VoiceSettings(enabled=True))
        with pytest.raises(NotFoundError):
            await service.create(
                actor=member_a,
                agent_slug=agent.slug,
                language=None,
                voice=None,
                scope=Scope(team_ids=frozenset({world["a"].id})),
            )
        created = await service.create(
            actor=member_a,
            agent_slug=agent.slug,
            language=None,
            voice=None,
            scope=Scope(team_ids=frozenset({world["b"].id})),
        )
        assert created.agent_id == agent.id

    async def test_memory_follows_the_agents_team(
        self, client: AsyncClient, tokens, member_a, session: AsyncSession, world
    ) -> None:
        end_user = f"person-{_suffix()}"
        for key in ("a", "b"):
            session.add(
                MemoryEntry(end_user=end_user, agent_id=world["agents"][key].id, text=f"from {key}")
            )
        session.add(MemoryEntry(end_user=end_user, agent_id=None, text="from nobody"))
        await session.flush()
        headers = auth_header(tokens, member_a)

        listed = await client.get(f"/api/v1/memory/entries?end_user={end_user}", headers=headers)
        assert listed.status_code == 200, listed.text
        assert {e["text"] for e in listed.json()} == {"from a", "from nobody"}

        theirs = next(
            e for e in (await session.execute(_entries(end_user))).scalars() if e.text == "from b"
        )
        forgot = await client.delete(f"/api/v1/memory/entries/{theirs.id}", headers=headers)
        assert forgot.status_code == 200
        assert forgot.json()["message"].startswith("No such memory")

        erased = await client.post(
            "/api/v1/memory/forget-all", headers=headers, json={"end_user": end_user, "query": "x"}
        )
        assert erased.status_code == 200, erased.text
        assert erased.json()["message"].startswith("Erased 2 ")
        left = [e.text for e in (await session.execute(_entries(end_user))).scalars()]
        assert left == ["from b"]

    async def test_dashboard_models_and_gateway_follow_scope(
        self, client: AsyncClient, tokens, member_a, superuser, session: AsyncSession, world
    ) -> None:
        from tests.api.test_gateway_timings import _row

        async def figures(user) -> tuple[int, int]:
            response = await client.get("/api/v1/dashboard", headers=auth_header(tokens, user))
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["models"] is not None and body["gateway"] is not None, body["unavailable"]
            return body["models"]["registered"], body["gateway"]["requests"]

        before_a, before_all = await figures(member_a), await figures(superuser)
        await _model(session, world["b"])
        for key, count in (("a", 2), ("b", 3)):
            team = world[key]
            for _ in range(count):
                session.add(_row(f"dash-{_suffix()}", team_id=team.id))
        await session.flush()
        after_a, after_all = await figures(member_a), await figures(superuser)

        assert after_a[0] - before_a[0] == 0, "team B's model is not counted for team A"
        assert after_all[0] - before_all[0] == 1
        assert after_a[1] - before_a[1] == 2
        assert after_all[1] - before_all[1] == 5


def _entries(end_user: str):
    from sqlalchemy import select

    return select(MemoryEntry).where(MemoryEntry.end_user == end_user)


async def _version_id(session: AsyncSession, agent: Agent):
    from sqlalchemy import select

    return (
        await session.execute(select(AgentVersion.id).where(AgentVersion.agent_id == agent.id))
    ).scalar_one()
