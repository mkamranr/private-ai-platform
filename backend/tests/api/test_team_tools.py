"""Tools can belong to a team (expansion E35).

Until E35 tools were a platform catalogue: any agent could be granted any tool, so a
tool built for one team (its database, its ticketing system) could be wired into
another team's agent. Now a tool may have an owning team, like a model or knowledge
base, and an agent may use only shared tools and its own team's: checked on save, on a
move and every time it runs.

Also covers the E35 fix to ``PUT /scope``, which refused ``workflow`` before.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.agents import AgentTool, Tool
from app.models.workflows import Workflow
from tests.api.conftest import _user_with
from tests.api.test_agents import _make_tool
from tests.api.test_team_scoping_e18 import EXTRA, _agent, _agent_body
from tests.api.test_teams import EVERYTHING, _member, world  # noqa: F401
from tests.api.test_workflows import ALL as WORKFLOW_PERMS
from tests.conftest import auth_header

TOOL_PERMS = [Perm.TOOL_VIEW, Perm.TOOL_MANAGE, Perm.TOOL_EXECUTE]


@pytest.fixture
async def builder_a(session: AsyncSession, settings, world: dict[str, Any]):
    """A member of team A who may build agents and manage tools."""
    user = await _user_with(
        session, settings, [*EVERYTHING, *EXTRA, *WORKFLOW_PERMS, *TOOL_PERMS], name="e35a"
    )
    await _member(session, world["a"], user)
    return user


@pytest.fixture
async def tools(session: AsyncSession, world: dict[str, Any]) -> dict[str, Tool]:
    """One tool shared, one in team A, one in team B."""
    out = {}
    for key, team in (("shared", None), ("a", world["a"]), ("b", world["b"])):
        tool = await _make_tool(session)
        tool.team_id = team.id if team else None
        out[key] = tool
    await session.flush()
    return out


class TestCatalogue:
    async def test_the_list_shows_shared_tools_and_your_teams(
        self, client: AsyncClient, tokens, builder_a, tools: dict[str, Tool]
    ) -> None:
        response = await client.get("/api/v1/tools", headers=auth_header(tokens, builder_a))
        assert response.status_code == 200, response.text
        listed = {t["id"]: t for t in response.json()}
        assert str(tools["shared"].id) in listed and str(tools["a"].id) in listed
        assert str(tools["b"].id) not in listed
        assert listed[str(tools["a"].id)]["team_id"] == str(tools["a"].team_id)

    async def test_another_teams_tool_is_404_to_change(
        self, client: AsyncClient, tokens, builder_a, tools: dict[str, Tool]
    ) -> None:
        headers = auth_header(tokens, builder_a)
        other = tools["b"].id
        assert (
            await client.put(f"/api/v1/tools/{other}", headers=headers, json={})
        ).status_code == 404
        assert (
            await client.post(f"/api/v1/tools/{other}/test", headers=headers)
        ).status_code == 404
        assert (await client.delete(f"/api/v1/tools/{other}", headers=headers)).status_code == 404

    async def test_a_tool_can_be_registered_into_your_team_only(
        self, client: AsyncClient, tokens, builder_a, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, builder_a)
        body = {
            "name": f"e35-{world['a'].id.hex[:8]}",
            "display_name": "Team tool",
            "description": "Looks things up for team A.",
            "type": "INTERNAL",
            "required_permission": Perm.TOOL_EXECUTE,
        }
        created = await client.post(
            "/api/v1/tools", headers=headers, json={**body, "team_id": str(world["a"].id)}
        )
        assert created.status_code == 201, created.text
        assert created.json()["team_id"] == str(world["a"].id)

        refused = await client.post(
            "/api/v1/tools",
            headers=headers,
            json={**body, "name": body["name"] + "-b", "team_id": str(world["b"].id)},
        )
        assert refused.status_code == 403, refused.text


class TestAgentsUseOnlyTheirTeamsTools:
    async def test_a_shared_agent_cannot_be_granted_a_team_tool(
        self, client: AsyncClient, tokens, builder_a, world, tools: dict[str, Tool]
    ) -> None:
        headers = auth_header(tokens, builder_a)
        model = world["aliases"]["shared"].alias
        refused = await client.post(
            "/api/v1/agents",
            headers=headers,
            json=_agent_body(model, tool_ids=[str(tools["a"].id), str(tools["shared"].id)]),
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["uses"] == [f"tool {tools['a'].name!r}"]

        allowed = await client.post(
            "/api/v1/agents",
            headers=headers,
            json=_agent_body(
                model,
                team_id=str(world["a"].id),
                tool_ids=[str(tools["a"].id), str(tools["shared"].id)],
            ),
        )
        assert allowed.status_code == 201, allowed.text

    async def test_a_tool_you_cannot_see_is_404(
        self, client: AsyncClient, tokens, builder_a, world, tools: dict[str, Tool]
    ) -> None:
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, builder_a),
            json=_agent_body(
                world["aliases"]["shared"].alias,
                team_id=str(world["a"].id),
                tool_ids=[str(tools["b"].id)],
            ),
        )
        assert response.status_code == 404, response.text

    async def test_a_new_version_is_checked_too(
        self, client: AsyncClient, tokens, builder_a, world, session, tools: dict[str, Tool]
    ) -> None:
        agent = await _agent(session, None, world["aliases"]["shared"].alias)
        response = await client.put(
            f"/api/v1/agents/{agent.id}",
            headers=auth_header(tokens, builder_a),
            json={"tool_ids": [str(tools["a"].id)]},
        )
        assert response.status_code == 422, response.text

    async def test_moving_an_agent_away_from_its_tool_is_refused(
        self, client: AsyncClient, tokens, builder_a, world, session, tools: dict[str, Tool]
    ) -> None:
        agent = await _agent(session, world["a"].id, world["aliases"]["shared"].alias)
        version_id = await _current_version_id(session, agent.id)
        session.add(AgentTool(agent_version_id=version_id, tool_id=tools["a"].id))
        await session.flush()

        response = await client.put(
            "/api/v1/scope",
            headers=auth_header(tokens, builder_a),
            json={"resource_type": "agent", "resource_id": str(agent.id), "team_id": None},
        )
        assert response.status_code == 409, response.text
        assert f"tool {tools['a'].name!r}" in response.json()["error"]["details"]["uses"]

    async def test_a_run_is_refused_once_its_tool_moved_to_another_team(
        self, client: AsyncClient, tokens, superuser, world, session, tools: dict[str, Tool]
    ) -> None:
        agent = await _agent(session, None, world["models"]["shared"].name)
        version_id = await _current_version_id(session, agent.id)
        session.add(AgentTool(agent_version_id=version_id, tool_id=tools["shared"].id))
        tools["shared"].team_id = world["b"].id
        await session.flush()

        response = await client.post(
            f"/api/v1/agents/{agent.id}/execute",
            headers=auth_header(tokens, superuser),
            json={"message": "hi"},
        )
        assert response.status_code == 409, response.text
        assert f"tool {tools['shared'].name!r}" in response.json()["error"]["message"]


class TestMoving:
    async def test_a_tool_moves_with_put_scope(
        self, client: AsyncClient, tokens, builder_a, world, session, tools: dict[str, Tool]
    ) -> None:
        response = await client.put(
            "/api/v1/scope",
            headers=auth_header(tokens, builder_a),
            json={
                "resource_type": "tool",
                "resource_id": str(tools["shared"].id),
                "team_id": str(world["a"].id),
            },
        )
        assert response.status_code == 200, response.text
        await session.refresh(tools["shared"])
        assert tools["shared"].team_id == world["a"].id

    async def test_a_workflow_moves_with_put_scope(
        self, client: AsyncClient, tokens, builder_a, world, session
    ) -> None:
        """Refused with 422 before E35: `workflow` was missing from the request schema."""
        workflow = Workflow(name=f"e35-{world['a'].id.hex[:8]}", display_name="E35", draft={})
        session.add(workflow)
        await session.flush()
        response = await client.put(
            "/api/v1/scope",
            headers=auth_header(tokens, builder_a),
            json={
                "resource_type": "workflow",
                "resource_id": str(workflow.id),
                "team_id": str(world["a"].id),
            },
        )
        assert response.status_code == 200, response.text
        await session.refresh(workflow)
        assert workflow.team_id == world["a"].id

    async def test_a_team_owning_a_tool_cannot_be_deleted(
        self, client: AsyncClient, tokens, superuser, world, tools: dict[str, Tool]
    ) -> None:
        response = await client.delete(
            f"/api/v1/teams/{world['b'].id}", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["details"]["resources"].get("tools") == 1


async def _current_version_id(session: AsyncSession, agent_id):
    from sqlalchemy import select

    from app.models.agents import AgentVersion

    return (
        await session.execute(select(AgentVersion.id).where(AgentVersion.agent_id == agent_id))
    ).scalar_one()
