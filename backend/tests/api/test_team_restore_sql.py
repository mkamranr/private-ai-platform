"""E68: putting a team's setup back from a backup, run against a real database.

As in ``test_scoped_restore_sql.py``: the staging tables are filled from the live rows
("taking the backup"), the live rows are then changed, and ``team_apply_sql`` has to put
back what was staged, without touching what the team created since or bringing back a
withdrawn API key.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import Agent, AgentRun, AgentTool, AgentVersion, RunState, Tool
from app.models.chargeback import CostBudget
from app.models.models_registry import ApiClient, ApiKey
from app.models.operations import GatewayQuota
from app.models.teams import Team, TeamMembership
from app.models.workflows import Workflow, WorkflowVersion
from tests.unit.test_release_tooling import load

pytestmark = pytest.mark.asyncio

backup = load("backup")


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def make_team(session: AsyncSession, owner) -> dict[str, Any]:
    """A team with one of everything a team restore covers."""
    tag = _tag()
    team = Team(name=f"Restore {tag}", slug=f"restore-{tag}")
    session.add(team)
    await session.flush()
    session.add(TeamMembership(team_id=team.id, user_id=owner.id))
    tool = Tool(
        name=f"tool-{tag}",
        display_name="Lookup",
        description="Looks things up.",
        type="INTERNAL",
        parameters_schema={"type": "object"},
        required_permission="tool.execute",
        team_id=team.id,
        config={"handler": "echo"},
    )
    shared = Tool(
        name=f"shared-{tag}",
        display_name="Shared",
        description="Belongs to nobody.",
        type="INTERNAL",
        parameters_schema={"type": "object"},
        required_permission="tool.execute",
        config={"handler": "echo"},
    )
    agent = Agent(
        slug=f"agent-{tag}",
        display_name="Helper",
        owner_id=owner.id,
        current_version=1,
        team_id=team.id,
    )
    session.add_all([tool, shared, agent])
    await session.flush()
    version = AgentVersion(agent_id=agent.id, version=1, system_prompt="Help.", model="m")
    session.add(version)
    await session.flush()
    session.add_all(
        [
            AgentTool(agent_version_id=version.id, tool_id=tool.id),
            AgentTool(agent_version_id=version.id, tool_id=shared.id),
        ]
    )
    workflow = Workflow(name=f"wf-{tag}", display_name="Nightly", team_id=team.id)
    client = ApiClient(name=f"client-{tag}", team_id=team.id)
    session.add_all([workflow, client])
    await session.flush()
    session.add(WorkflowVersion(workflow_id=workflow.id, version=1, definition={"nodes": []}))
    kept = ApiKey(client_id=client.id, name="kept", prefix=f"k{tag}", key_hash=uuid.uuid4().hex)
    gone = ApiKey(client_id=client.id, name="gone", prefix=f"g{tag}", key_hash=uuid.uuid4().hex)
    session.add_all([kept, gone])
    await session.flush()
    session.add_all(
        [
            GatewayQuota(scope="team", subject=str(team.id), daily_tokens=1000),
            GatewayQuota(scope="api_key", subject=str(kept.id), daily_tokens=50),
            CostBudget(scope="team", subject=str(team.id), monthly_amount=Decimal("100")),
        ]
    )
    await session.flush()
    return {
        "team": team,
        "tool": tool,
        "shared": shared,
        "agent": agent,
        "version": version,
        "workflow": workflow,
        "client": client,
        "kept": kept,
        "gone": gone,
    }


async def columns(session: AsyncSession) -> dict[str, list[str]]:
    names = ", ".join(backup._literal(t) for t in backup.TEAM_NAMES)
    rows = (await session.execute(text(backup.COLUMNS_SQL.format(names=names)))).all()
    live = {row[0]: row[1].split(",") for row in rows}
    return backup.common_columns(live, live, backup.TEAM_NAMES)


async def take_backup(session: AsyncSession, team_id: uuid.UUID) -> dict[str, list[str]]:
    common = await columns(session)
    for statement in backup.team_stage_sql(common):
        await session.execute(text(statement))
    values = backup._team_values(str(team_id))
    for table, where, _ in backup.TEAM_TABLES:
        cols = ", ".join(backup._ident(c) for c in common[table])
        await session.execute(
            text(
                f"INSERT INTO stage_{table} ({cols}) SELECT {cols} FROM public.{table} "  # noqa: S608 (test-built SQL from constants)
                f"WHERE {where.format(**values)}"
            )
        )
    return common


async def apply(session: AsyncSession, team_id: uuid.UUID, common: dict[str, Any]) -> None:
    names = ", ".join(backup._literal(t) for t in backup.TEAM_NAMES)
    fks = [
        (r[0], r[1], r[2], r[3], r[4] == "true")
        for r in (await session.execute(text(backup.FOREIGN_KEYS_SQL.format(names=names)))).all()
    ]
    uniques = [
        (r[0], r[1].split(","))
        for r in (await session.execute(text(backup.UNIQUES_SQL.format(names=names)))).all()
    ]
    for statement in backup.team_apply_sql(str(team_id), common, fks, uniques):
        await session.execute(text(statement))
    session.expunge_all()


async def test_changed_rows_are_put_back_and_new_ones_kept(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    team, agent = made["team"], made["agent"]
    staged = await take_backup(session, team.id)

    # Since the backup: the agent was edited and ran, a second agent and workflow were
    # made, the team's quota was raised and its budget deleted.
    await session.execute(update(Agent).where(Agent.id == agent.id).values(display_name="Edited"))
    session.add(
        AgentRun(
            agent_id=agent.id,
            agent_version_id=made["version"].id,
            state=RunState.COMPLETED,
            input="hi",
        )
    )
    newer = Agent(slug=f"new-{_tag()}", display_name="New", current_version=1, team_id=team.id)
    session.add(newer)
    await session.execute(
        update(GatewayQuota).where(GatewayQuota.subject == str(team.id)).values(daily_tokens=9)
    )
    await session.execute(delete(CostBudget).where(CostBudget.subject == str(team.id)))
    await session.flush()

    await apply(session, team.id, staged)

    restored = await session.get(Agent, agent.id)
    assert restored is not None and restored.display_name == "Helper"
    assert await session.get(Agent, newer.id) is not None  # made since: kept
    runs = (await session.execute(select(AgentRun).where(AgentRun.agent_id == agent.id))).all()
    assert len(runs) == 1  # history still points at the same agent
    quota = (
        await session.execute(
            select(GatewayQuota).where(
                GatewayQuota.scope == "team", GatewayQuota.subject == str(team.id)
            )
        )
    ).scalar_one()
    assert quota.daily_tokens == 1000
    assert (
        await session.execute(select(CostBudget).where(CostBudget.subject == str(team.id)))
    ).scalar_one().monthly_amount == Decimal("100")


async def test_withdrawn_keys_stay_withdrawn(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    staged = await take_backup(session, made["team"].id)
    revoked_at = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
    await session.execute(
        update(ApiKey).where(ApiKey.id == made["kept"].id).values(revoked_at=revoked_at)
    )
    await session.execute(delete(ApiKey).where(ApiKey.id == made["gone"].id))
    await session.flush()

    await apply(session, made["team"].id, staged)

    kept = await session.get(ApiKey, made["kept"].id)
    gone = await session.get(ApiKey, made["gone"].id)
    assert kept is not None and kept.revoked_at == revoked_at  # revoked since: stays
    assert gone is not None and gone.revoked_at is not None  # deleted since: back, revoked


async def test_a_deleted_team_comes_back_whole(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    team = made["team"]
    staged = await take_backup(session, team.id)
    # Everything it owned had to go before the team could be deleted.
    await session.execute(
        delete(GatewayQuota).where(GatewayQuota.subject.in_([str(team.id), str(made["kept"].id)]))
    )
    await session.execute(delete(CostBudget).where(CostBudget.subject == str(team.id)))
    await session.execute(delete(ApiClient).where(ApiClient.id == made["client"].id))
    await session.execute(delete(Workflow).where(Workflow.id == made["workflow"].id))
    await session.execute(delete(Agent).where(Agent.id == made["agent"].id))
    await session.execute(delete(Tool).where(Tool.id == made["tool"].id))
    await session.execute(delete(Team).where(Team.id == team.id))
    await session.flush()

    await apply(session, team.id, staged)

    assert (await session.get(Team, team.id)) is not None
    member = (
        await session.execute(select(TeamMembership).where(TeamMembership.team_id == team.id))
    ).scalar_one()
    assert member.user_id == superuser.id
    assert (await session.get(Agent, made["agent"].id)).team_id == team.id
    assert (
        await session.get(
            WorkflowVersion,
            (
                await session.execute(
                    select(WorkflowVersion.id).where(
                        WorkflowVersion.workflow_id == made["workflow"].id
                    )
                )
            ).scalar_one(),
        )
    ) is not None
    grants = (
        (
            await session.execute(
                select(AgentTool.tool_id).where(AgentTool.agent_version_id == made["version"].id)
            )
        )
        .scalars()
        .all()
    )
    assert set(grants) == {made["tool"].id, made["shared"].id}


async def test_a_grant_of_a_tool_deleted_since_is_dropped(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    staged = await take_backup(session, made["team"].id)
    await session.execute(delete(Tool).where(Tool.id == made["shared"].id))
    await session.flush()

    await apply(session, made["team"].id, staged)
    grants = (
        (
            await session.execute(
                select(AgentTool.tool_id).where(AgentTool.agent_version_id == made["version"].id)
            )
        )
        .scalars()
        .all()
    )
    assert grants == [made["tool"].id]


async def test_a_name_taken_since_refuses(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    staged = await take_backup(session, made["team"].id)
    # The workflow was deleted and someone else used its name.
    await session.execute(delete(Workflow).where(Workflow.id == made["workflow"].id))
    session.add(Workflow(name=made["workflow"].name, display_name="Someone else's"))
    await session.flush()

    with pytest.raises(DBAPIError, match="SCOPED-REFUSED"):
        await apply(session, made["team"].id, staged)
