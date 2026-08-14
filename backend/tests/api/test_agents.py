"""Agents, the §10 tool pipeline, and durable approval (M10-M14).

Weighted towards the pipeline. The registries are bookkeeping; the pipeline is where a
mistake turns an agent into a privilege-escalation path, and where a mistake is silent.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.tools import RiskLevel, ToolResult, ToolType
from app.core.permissions import Permission as Perm
from app.models.agents import (
    Agent,
    AgentRun,
    AgentTool,
    AgentVersion,
    ApprovalState,
    RunState,
    Tool,
)
from app.models.auth import User
from tests.api.conftest import _user_with
from tests.conftest import auth_header


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
async def agent_admin(session: AsyncSession, settings) -> User:
    return await _user_with(
        session,
        settings,
        [
            Perm.AGENT_VIEW,
            Perm.AGENT_CREATE,
            Perm.AGENT_EDIT,
            Perm.AGENT_DELETE,
            Perm.AGENT_EXECUTE,
            Perm.TOOL_VIEW,
            Perm.TOOL_MANAGE,
            Perm.TOOL_EXECUTE,
            Perm.TOOL_APPROVE,
            Perm.SKILL_VIEW,
            Perm.SKILL_MANAGE,
            Perm.MCP_VIEW,
            Perm.MCP_MANAGE,
        ],
        name="agentadmin",
    )


async def _make_tool(
    session: AsyncSession,
    *,
    risk: str = "LOW",
    permission: str = Perm.TOOL_EXECUTE,
    enabled: bool = True,
    tool_type: str = "INTERNAL",
) -> Tool:
    tool = Tool(
        name=f"tool-{uuid.uuid4().hex[:8]}",
        display_name="Test Tool",
        description="A tool for tests.",
        type=tool_type,
        parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        required_permission=permission,
        risk_level=risk,
        enabled=enabled,
        config={"handler": "echo"},
    )
    session.add(tool)
    await session.flush()
    return tool


async def _make_agent(
    session: AsyncSession, *, tools: list[Tool], owner: User, max_iterations: int = 4
) -> tuple[Agent, AgentVersion]:
    agent = Agent(
        slug=f"agent-{uuid.uuid4().hex[:8]}",
        display_name="Test Agent",
        owner_id=owner.id,
        current_version=1,
    )
    session.add(agent)
    await session.flush()

    version = AgentVersion(
        agent_id=agent.id,
        version=1,
        system_prompt="You are a test agent.",
        model="test-model",
        max_iterations=max_iterations,
    )
    session.add(version)
    await session.flush()
    for tool in tools:
        session.add(AgentTool(agent_version_id=version.id, tool_id=tool.id))
    await session.flush()
    return agent, version


async def _make_run(
    session: AsyncSession,
    agent: Agent,
    version: AgentVersion,
    *,
    user: User,
    permissions: list[str],
) -> AgentRun:
    run = AgentRun(
        agent_id=agent.id,
        agent_version_id=version.id,
        user_id=user.id,
        state=RunState.RUNNING,
        input="hello",
        user_permissions=permissions,
    )
    session.add(run)
    await session.flush()
    return run


def _pipeline(session: AsyncSession, settings, executors=None):
    from app.repositories.agents import ToolExecutionRepository
    from app.repositories.audit import AuditRepository
    from app.services.audit import AuditService
    from app.services.tool_pipeline import ToolPipeline

    async def echo(arguments: dict) -> str:
        return f"echoed {arguments}"

    from app.services.tool_executors import InternalToolExecutor

    return ToolPipeline(
        settings,
        ToolExecutionRepository(session),
        AuditService(AuditRepository(session)),
        executors or {ToolType.INTERNAL: InternalToolExecutor({"echo": echo})},
    )


# ---------------------------------------------------------------------------
# The §10 pipeline
# ---------------------------------------------------------------------------
class TestToolPipeline:
    async def test_granted_and_permitted_executes(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={"q": "x"}, granted_tool_ids={tool.id}
        )
        assert outcome.result is not None
        assert outcome.result.success is True

    async def test_permission_check_is_an_intersection_not_a_union(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """**The most important test in this file.**

        The agent is granted the tool. The *user* is not permitted it. Union — "the agent
        is allowed, so the call is allowed" — is the natural implementation and it turns
        every agent into a confused deputy: a way to reach a tool you could not call
        yourself.
        """
        tool = await _make_tool(session, permission="secret.thing")
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is None
        assert outcome.denial is not None
        assert outcome.denial.audit_detail == "user_lacks_permission"
        # The refusal names the permission, so the model can tell the user what to ask for.
        assert "secret.thing" in outcome.denial.reason

    async def test_tool_the_agent_was_not_granted_is_refused(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """The other half of the intersection: a user permitted to use a tool cannot reach
        it through an agent that was never given it."""
        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids=set()
        )
        assert outcome.denial is not None
        assert outcome.denial.audit_detail == "not_granted_to_agent"

    @pytest.mark.parametrize("tool_type", ["PYTHON", "COMMAND"])
    async def test_disabled_tool_types_never_execute(
        self, session: AsyncSession, settings, agent_admin: User, tool_type: str
    ) -> None:
        """§25 over §M12. Registerable so an operator can catalogue what exists; refused
        at execution, even when the agent is granted it and the user is permitted it —
        which is exactly the configuration that would otherwise open a shell."""
        tool = await _make_tool(session, tool_type=tool_type)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.denial is not None
        assert outcome.denial.audit_detail == "tool_type_disabled"

    async def test_no_executor_exists_for_disabled_types(self) -> None:
        """Belt and braces: even if the pipeline check were removed, dispatch would fail.
        Two independent reasons a prompt-injected instruction cannot reach a shell."""
        from app.services.tool_executors import build_executors

        table = build_executors(cipher=None)  # type: ignore[arg-type]
        assert ToolType.PYTHON not in table
        assert ToolType.COMMAND not in table

    async def test_disabled_tool_is_refused(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        tool = await _make_tool(session, enabled=False)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )
        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.denial is not None
        assert outcome.denial.audit_detail == "tool_disabled"

    @pytest.mark.parametrize("risk", ["HIGH", "CRITICAL"])
    async def test_high_risk_suspends_rather_than_running(
        self, session: AsyncSession, settings, agent_admin: User, risk: str
    ) -> None:
        tool = await _make_tool(session, risk=risk)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is None
        assert outcome.pending is not None
        assert outcome.pending.approval_state == ApprovalState.PENDING

    @pytest.mark.parametrize("risk", ["LOW", "MEDIUM"])
    async def test_low_risk_runs_without_asking(
        self, session: AsyncSession, settings, agent_admin: User, risk: str
    ) -> None:
        tool = await _make_tool(session, risk=risk)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )
        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is not None

    async def test_every_call_is_recorded_including_refusals(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """A row written only on success would record none of the interesting cases —
        and an audit trail missing exactly the refusals reads as "nothing was refused"."""
        from app.repositories.agents import ToolExecutionRepository

        denied = await _make_tool(session, permission="nope.nope")
        allowed = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[denied, allowed], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        pipeline = _pipeline(session, settings)
        await pipeline.invoke(
            run=run, tool=denied, arguments={}, granted_tool_ids={denied.id, allowed.id}
        )
        await pipeline.invoke(
            run=run, tool=allowed, arguments={}, granted_tool_ids={denied.id, allowed.id}
        )

        rows = await ToolExecutionRepository(session).list_for_run(run.id)
        assert len(rows) == 2
        assert {r.approval_state for r in rows} == {
            ApprovalState.REJECTED,
            ApprovalState.NOT_REQUIRED,
        }

    async def test_an_executor_that_raises_does_not_kill_the_run(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """An executor bug is the executor's problem. The agent gets a normal failed
        result to reason about; a raise here would let one broken tool end every run
        that touches it."""
        from app.core.interfaces.tools import ToolExecutor

        class Exploding(ToolExecutor):
            @property
            def tool_type(self) -> ToolType:
                return ToolType.INTERNAL

            async def execute(self, invocation: Any) -> ToolResult:
                raise RuntimeError("boom")

            async def validate(self, tool: Any) -> None:
                return None

        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings, {ToolType.INTERNAL: Exploding()}).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is not None
        assert outcome.result.success is False
        assert "boom" in (outcome.result.error or "")

    async def test_large_results_are_truncated_and_marked(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """A tool returning a 40 MB listing would blow the context window. Truncation is
        recorded so a trace never implies the model saw more than it did."""
        from app.core.interfaces.tools import ToolExecutor
        from app.services.tool_pipeline import MAX_RESULT_CHARS

        class Verbose(ToolExecutor):
            @property
            def tool_type(self) -> ToolType:
                return ToolType.INTERNAL

            async def execute(self, invocation: Any) -> ToolResult:
                return ToolResult(
                    success=True, content="x" * (MAX_RESULT_CHARS + 5000), duration_ms=1.0
                )

            async def validate(self, tool: Any) -> None:
                return None

        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )
        outcome = await _pipeline(session, settings, {ToolType.INTERNAL: Verbose()}).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )
        assert outcome.result is not None
        assert outcome.result.truncated is True
        assert "[truncated]" in outcome.result.content


# ---------------------------------------------------------------------------
# Registry (M10-M13)
# ---------------------------------------------------------------------------
class TestRegistry:
    async def test_tool_needs_a_permission(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        """§M12. A tool without one would be callable by anyone the agent is exposed to."""
        response = await client.post(
            "/api/v1/tools",
            headers=auth_header(tokens, agent_admin),
            json={
                "name": "nameless",
                "display_name": "Nameless",
                "description": "d",
                "type": "REST",
            },
        )
        assert response.status_code == 422

    async def test_agent_starts_with_no_tools(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        """§10: agents have no tool access by default."""
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, agent_admin),
            json={
                "slug": f"bare-{uuid.uuid4().hex[:6]}",
                "display_name": "Bare",
                "system_prompt": "You are bare.",
                "model": "x",
            },
        )
        assert response.status_code == 201
        assert response.json()["version"]["tools"] == []

    async def test_editing_publishes_a_version_and_keeps_the_old_one(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        """An agent edited after an incident otherwise makes its own trace
        unreproducible: the prompt in the database is no longer the prompt that ran."""
        created = (
            await client.post(
                "/api/v1/agents",
                headers=auth_header(tokens, agent_admin),
                json={
                    "slug": f"versioned-{uuid.uuid4().hex[:6]}",
                    "display_name": "Versioned",
                    "system_prompt": "First prompt.",
                    "model": "x",
                },
            )
        ).json()

        updated = await client.put(
            f"/api/v1/agents/{created['id']}",
            headers=auth_header(tokens, agent_admin),
            json={"system_prompt": "Second prompt.", "change_note": "Tightened scope."},
        )
        assert updated.status_code == 200
        assert updated.json()["current_version"] == 2

        history = (
            await client.get(
                f"/api/v1/agents/{created['id']}/versions",
                headers=auth_header(tokens, agent_admin),
            )
        ).json()
        assert [v["version"] for v in history] == [2, 1]
        assert history[1]["system_prompt"] == "First prompt.", "version 1 was mutated"

    async def test_partial_update_does_not_blank_tool_grants(
        self, client: AsyncClient, tokens, agent_admin: User, session: AsyncSession
    ) -> None:
        """The natural bug: treat every omitted field as "None means empty" and an
        unrelated edit silently strips the agent's tools."""
        tool = await _make_tool(session)
        created = (
            await client.post(
                "/api/v1/agents",
                headers=auth_header(tokens, agent_admin),
                json={
                    "slug": f"keeps-{uuid.uuid4().hex[:6]}",
                    "display_name": "Keeps",
                    "system_prompt": "p",
                    "model": "x",
                    "tool_ids": [str(tool.id)],
                },
            )
        ).json()
        assert created["version"]["tools"] == [tool.name]

        updated = await client.put(
            f"/api/v1/agents/{created['id']}",
            headers=auth_header(tokens, agent_admin),
            json={"display_name": "Renamed"},
        )
        assert updated.json()["version"]["tools"] == [tool.name]

    async def test_slug_is_validated(self, client: AsyncClient, tokens, agent_admin: User) -> None:
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, agent_admin),
            json={
                "slug": "Not A Slug",
                "display_name": "x",
                "system_prompt": "p",
                "model": "x",
            },
        )
        assert response.status_code == 422

    async def test_version_cannot_reference_a_missing_tool(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        """A dangling reference would fail at run time, with a user waiting."""
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, agent_admin),
            json={
                "slug": f"dangling-{uuid.uuid4().hex[:6]}",
                "display_name": "x",
                "system_prompt": "p",
                "model": "x",
                "tool_ids": [str(uuid.uuid4())],
            },
        )
        assert response.status_code == 422

    async def test_creating_an_agent_requires_create_not_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.AGENT_VIEW], name="agentviewer")
        response = await client.post(
            "/api/v1/agents",
            headers=auth_header(tokens, viewer),
            json={"slug": "x-y-z", "display_name": "x", "system_prompt": "p", "model": "x"},
        )
        assert response.status_code == 403

    async def test_skills_append_to_the_prompt_rather_than_replacing_it(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """The agent's own instructions set its character and boundaries; a shared skill
        adds a capability within them. The other order would let a skill silently
        override an agent's constraints."""
        from app.models.agents import AgentSkill, Skill
        from app.services.agent_registry import _compose_prompt

        _, version = await _make_agent(session, tools=[], owner=agent_admin)
        skill = Skill(
            name=f"skill-{uuid.uuid4().hex[:6]}",
            display_name="Directory Lookups",
            description="How to read the directory.",
            instructions="Always state the employee id.",
        )
        session.add(skill)
        await session.flush()
        session.add(AgentSkill(agent_version_id=version.id, skill_id=skill.id))
        await session.flush()

        resolved = await _reload_version(session, version.id)
        prompt = _compose_prompt(resolved)
        assert prompt.startswith("You are a test agent.")
        assert "Always state the employee id." in prompt


# ---------------------------------------------------------------------------
# Approval (§10, §M24)
# ---------------------------------------------------------------------------
class TestApproval:
    async def test_approving_requires_tool_approve_not_tool_execute(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """Deliberately separate (§10, §M24): collapsing them would let any tool user
        approve their own privileged call."""
        executor = await _user_with(
            session, settings, [Perm.TOOL_EXECUTE, Perm.AGENT_EXECUTE], name="toolexec"
        )
        response = await client.post(
            f"/api/v1/runs/{uuid.uuid4()}/approve",
            headers=auth_header(tokens, executor),
            json={"approved": True},
        )
        assert response.status_code == 403

    async def test_seeing_the_queue_requires_tool_approve(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.AGENT_VIEW], name="queueviewer")
        response = await client.get(
            "/api/v1/runs/pending-approvals", headers=auth_header(tokens, viewer)
        )
        assert response.status_code == 403

    async def test_pending_approvals_is_not_parsed_as_a_run_id(
        self, client: AsyncClient, tokens, agent_admin: User
    ) -> None:
        """Route ordering: FastAPI matches in declaration order, so a literal path after
        `/runs/{run_id}` would be rejected as a malformed UUID."""
        response = await client.get(
            "/api/v1/runs/pending-approvals", headers=auth_header(tokens, agent_admin)
        )
        assert response.status_code == 200

    async def test_approving_a_run_that_is_not_waiting_is_a_conflict(
        self, client: AsyncClient, tokens, agent_admin: User, session: AsyncSession
    ) -> None:
        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )
        response = await client.post(
            f"/api/v1/runs/{run.id}/approve",
            headers=auth_header(tokens, agent_admin),
            json={"approved": True},
        )
        assert response.status_code == 409

    async def test_the_suspended_state_lives_in_the_database(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """Why a run survives a restart: everything needed to continue it is a row, not
        an in-memory handle. This is the property the whole runtime choice turns on."""
        tool = await _make_tool(session, risk="HIGH")
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )

        outcome = await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={"q": "x"}, granted_tool_ids={tool.id}
        )
        assert outcome.pending is not None

        from app.repositories.agents import ToolExecutionRepository

        # Found by a query, with no reference to the object that created it.
        found = await ToolExecutionRepository(session).pending_for_run(run.id)
        assert found is not None
        assert found.tool_name == tool.name
        assert found.arguments == {"q": "x"}

    async def test_expiry_is_distinct_from_rejection(
        self, session: AsyncSession, settings, agent_admin: User
    ) -> None:
        """Nobody decided. An audit that recorded a timeout as a refusal would attribute
        it to a person who never saw it."""
        import datetime as dt

        from app.repositories.agents import ToolExecutionRepository

        tool = await _make_tool(session, risk="HIGH")
        agent, version = await _make_agent(session, tools=[tool], owner=agent_admin)
        run = await _make_run(
            session, agent, version, user=agent_admin, permissions=[Perm.TOOL_EXECUTE]
        )
        await _pipeline(session, settings).invoke(
            run=run, tool=tool, arguments={}, granted_tool_ids={tool.id}
        )

        repo = ToolExecutionRepository(session)
        expired = await repo.expire_older_than(dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
        assert expired == 1
        row = (await repo.list_for_run(run.id))[0]
        assert row.approval_state == ApprovalState.EXPIRED
        assert row.approver_id is None


# ---------------------------------------------------------------------------
# Agents through the gateway (§M17)
# ---------------------------------------------------------------------------
class TestAgentPseudoModels:
    async def test_agent_appears_in_the_catalogue(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair,
        agent_admin: User,
        serving_deployment,
    ) -> None:
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        _, secret = api_key_pair
        listed = {
            m["id"]
            for m in (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
            ).json()["data"]
        }
        assert f"agent:{agent.slug}" in listed

    async def test_disabled_agents_are_not_offered(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair,
        agent_admin: User,
    ) -> None:
        """A disabled agent in the picker produces a failure the user cannot act on."""
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        agent.enabled = False
        await session.flush()

        _, secret = api_key_pair
        listed = {
            m["id"]
            for m in (
                await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
            ).json()["data"]
        }
        assert f"agent:{agent.slug}" not in listed

    async def test_unknown_agent_is_404_with_suggestions(
        self,
        gateway_app,
        client: AsyncClient,
        api_key_pair,
    ) -> None:
        _, secret = api_key_pair
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": "agent:does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 404
        assert "available" in response.json()["error"]["details"]

    async def test_client_without_an_owner_cannot_run_agents(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair,
        agent_admin: User,
    ) -> None:
        """Refusing is the only safe answer. The alternative is picking some default
        identity and silently authorising tool calls as it."""
        agent, _ = await _make_agent(session, tools=[], owner=agent_admin)
        _, secret = api_key_pair  # this fixture's client has no owner

        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "model": f"agent:{agent.slug}",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 403
        assert "no owner" in response.json()["error"]["message"]

    async def test_agent_prefix_cannot_collide_with_a_model_name(self) -> None:
        """`:` is not permitted in a model name or an alias, so the namespaces are
        disjoint however many of each exist."""
        import re

        from app.schemas.models_registry import ModelRegisterRequest

        pattern = next(
            m.pattern
            for m in ModelRegisterRequest.model_fields["name"].metadata
            if hasattr(m, "pattern")
        )
        assert re.fullmatch(pattern, "agent") is not None
        assert re.fullmatch(pattern, "agent:thing") is None


async def _reload_version(session: AsyncSession, version_id: uuid.UUID) -> AgentVersion:
    from app.repositories.agents import AgentVersionRepository

    resolved = await AgentVersionRepository(session).get_resolved(version_id)
    assert resolved is not None
    return resolved


# ---------------------------------------------------------------------------
# MCP discovery (§M13)
# ---------------------------------------------------------------------------
class TestDiscovery:
    async def test_discovered_tools_arrive_disabled_and_high_risk(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**Discovery populates; it never grants.** Otherwise pointing the platform at a
        server would silently widen what every agent can do."""
        from app.models.agents import McpServer
        from app.repositories.agents import McpServerRepository, ToolRepository
        from app.repositories.audit import AuditRepository
        from app.services import agent_registry
        from app.services.agent_registry import DISCOVERED_RISK_LEVEL, McpRegistryService
        from app.services.audit import AuditService

        server = McpServer(
            name=f"srv-{uuid.uuid4().hex[:6]}", endpoint="http://mcp.invalid/", transport="HTTP"
        )
        session.add(server)
        await session.flush()

        async def fake_call_mcp(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "tools": [
                    {
                        "name": "delete_everything",
                        "description": "Removes all records.",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }

        monkeypatch.setattr(agent_registry, "call_mcp", fake_call_mcp)

        service = McpRegistryService(
            _settings(),
            McpServerRepository(session),
            ToolRepository(session),
            AuditService(AuditRepository(session)),
            cipher=None,  # type: ignore[arg-type]
        )
        result = await service.discover(
            server.id, actor=await _user_with(session, _settings(), [], name="mcpadmin")
        )
        assert result.created == 1

        created = (await ToolRepository(session).list_for_server(server.id))[0]
        assert created.enabled is False, "a discovered tool was enabled without review"
        assert created.risk_level == DISCOVERED_RISK_LEVEL
        # Namespaced: two servers may both offer `search`, and names are unique.
        assert created.name == f"{server.name}.delete_everything"

    async def test_a_server_offering_one_name_twice_does_not_crash_discovery(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`tools.name` is unique fleet-wide, so a repeated name must be folded, not inserted.

        The loop kept its `existing` map from before the scan and never added what it
        created, so a server listing the same tool twice built two rows with one name and
        died at flush — an IntegrityError surfacing as a 500, blamed on the platform rather
        than on the server that sent the duplicate.
        """
        from app.models.agents import McpServer
        from app.repositories.agents import McpServerRepository, ToolRepository
        from app.repositories.audit import AuditRepository
        from app.services import agent_registry
        from app.services.agent_registry import McpRegistryService
        from app.services.audit import AuditService

        server = McpServer(
            name=f"srv-{uuid.uuid4().hex[:6]}", endpoint="http://mcp.invalid/", transport="HTTP"
        )
        session.add(server)
        await session.flush()

        async def fake_call_mcp(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "tools": [
                    {
                        "name": "search",
                        "description": "First.",
                        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    },
                    {
                        "name": "search",
                        "description": "Duplicate.",
                        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    },
                ]
            }

        monkeypatch.setattr(agent_registry, "call_mcp", fake_call_mcp)

        service = McpRegistryService(
            _settings(),
            McpServerRepository(session),
            ToolRepository(session),
            AuditService(AuditRepository(session)),
            cipher=None,  # type: ignore[arg-type]
        )
        result = await service.discover(
            server.id, actor=await _user_with(session, _settings(), [], name="mcpdupe")
        )

        assert result.created == 1, "the duplicate was catalogued as a second tool"
        tools = await ToolRepository(session).list_for_server(server.id)
        assert len(tools) == 1
        # The later entry wins, the same way a re-scan refreshes an existing tool.
        assert tools[0].description == "Duplicate."

    async def test_a_name_already_taken_by_another_tool_is_skipped_not_inserted(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery looked for the name only among *this* server's tools.

        A tool registered by hand under the namespaced spelling is invisible to that
        lookup, so discovery inserted a second row with the same globally-unique name and
        died at flush. Refusing the one tool and saying so beats a 500, and beats silently
        repointing somebody's hand-made tool at an MCP endpoint.
        """
        from app.core.interfaces.tools import ToolType
        from app.models.agents import McpServer, Tool
        from app.repositories.agents import McpServerRepository, ToolRepository
        from app.repositories.audit import AuditRepository
        from app.services import agent_registry
        from app.services.agent_registry import McpRegistryService
        from app.services.audit import AuditService

        name = f"srv-{uuid.uuid4().hex[:6]}"
        server = McpServer(name=name, endpoint="http://mcp.invalid/", transport="HTTP")
        session.add(server)
        # Registered by hand, before the server existed, under the name discovery will want.
        session.add(
            Tool(
                name=f"{name}.search",
                display_name="hand-made",
                description="Registered by an operator, not by discovery.",
                type=ToolType.REST,
                parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                required_permission="tool.execute",
                risk_level="LOW",
                endpoint="http://elsewhere.invalid/",
            )
        )
        await session.flush()

        async def fake_call_mcp(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "tools": [
                    {
                        "name": "search",
                        "description": "MCP.",
                        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    }
                ]
            }

        monkeypatch.setattr(agent_registry, "call_mcp", fake_call_mcp)

        service = McpRegistryService(
            _settings(),
            McpServerRepository(session),
            ToolRepository(session),
            AuditService(AuditRepository(session)),
            cipher=None,  # type: ignore[arg-type]
        )
        result = await service.discover(
            server.id, actor=await _user_with(session, _settings(), [], name="mcpclash")
        )

        assert result.created == 0
        assert result.detail and "already" in result.detail.lower()
        # The operator's tool is untouched — same endpoint, same description.
        clash = await ToolRepository(session).get_by_name(f"{name}.search")
        assert clash is not None
        assert clash.endpoint == "http://elsewhere.invalid/"
        assert clash.mcp_server_id is None

    async def test_rediscovery_does_not_undo_operator_decisions(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operator reviewed the tool, enabled it and lowered its risk. Re-running
        discovery must refresh the description, not reset the review."""
        from app.models.agents import McpServer
        from app.repositories.agents import McpServerRepository, ToolRepository
        from app.repositories.audit import AuditRepository
        from app.services import agent_registry
        from app.services.agent_registry import McpRegistryService
        from app.services.audit import AuditService

        server = McpServer(
            name=f"srv-{uuid.uuid4().hex[:6]}", endpoint="http://mcp.invalid/", transport="HTTP"
        )
        session.add(server)
        await session.flush()

        async def fake_call_mcp(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "tools": [
                    {"name": "lookup", "description": "Updated description.", "inputSchema": {}}
                ]
            }

        monkeypatch.setattr(agent_registry, "call_mcp", fake_call_mcp)
        service = McpRegistryService(
            _settings(),
            McpServerRepository(session),
            ToolRepository(session),
            AuditService(AuditRepository(session)),
            cipher=None,  # type: ignore[arg-type]
        )
        actor = await _user_with(session, _settings(), [], name="mcpadmin2")

        await service.discover(server.id, actor=actor)
        tool = (await ToolRepository(session).list_for_server(server.id))[0]
        tool.enabled = True
        tool.risk_level = RiskLevel.LOW
        await session.flush()

        await service.discover(server.id, actor=actor)
        tool = (await ToolRepository(session).list_for_server(server.id))[0]
        assert tool.enabled is True, "re-discovery disabled a reviewed tool"
        assert tool.risk_level == RiskLevel.LOW, "re-discovery reset a reviewed risk level"
        assert tool.description == "Updated description."


def _settings():
    from app.config.settings import get_settings

    return get_settings()
