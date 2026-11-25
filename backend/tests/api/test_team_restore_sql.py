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

from app.models.agents import (
    Agent,
    AgentRun,
    AgentRunEvent,
    AgentTool,
    AgentVersion,
    ApprovalState,
    RunState,
    Tool,
    ToolExecution,
)
from app.models.chargeback import CostBudget
from app.models.models_registry import ApiClient, ApiKey, Model, ModelAlias, ModelStatus
from app.models.operations import GatewayQuota
from app.models.teams import Team, TeamMembership
from app.models.workflows import (
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStep,
    WorkflowVersion,
)
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


# ---------------------------------------------------------------------------
# E85: history, approvals, and the models and aliases the team owned
# ---------------------------------------------------------------------------
async def add_history(session: AsyncSession, made: dict[str, Any], owner) -> dict[str, Any]:
    team = made["team"]
    tag = _tag()
    model = Model(
        name=f"team-model-{tag}",
        display_name="Team model",
        type="LLM",
        storage_path="/data/models/t",
        runtime="mock",
        context_length=8192,
        status=ModelStatus.AVAILABLE,
        team_id=team.id,
    )
    shared = Model(
        name=f"shared-model-{tag}",
        display_name="Shared model",
        type="LLM",
        storage_path="/data/models/s",
        runtime="mock",
        context_length=8192,
        status=ModelStatus.AVAILABLE,
    )
    session.add_all([model, shared])
    await session.flush()
    alias = ModelAlias(alias=f"team-alias-{tag}", model_id=model.id, team_id=team.id)
    elsewhere = ModelAlias(alias=f"team-shared-{tag}", model_id=shared.id, team_id=team.id)
    session.add_all([alias, elsewhere])
    run = AgentRun(
        agent_id=made["agent"].id,
        agent_version_id=made["version"].id,
        user_id=owner.id,
        state=RunState.WAITING_FOR_APPROVAL,
        input="Delete the file?",
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            AgentRunEvent(run_id=run.id, sequence=1, type="RUN_STARTED", payload={}),
            ToolExecution(
                run_id=run.id,
                tool_id=made["tool"].id,
                tool_name="lookup",
                arguments={},
                approval_state=ApprovalState.APPROVED,
                approver_id=owner.id,
            ),
        ]
    )
    wf_run = WorkflowRun(
        workflow_id=made["workflow"].id,
        version=1,
        definition={"nodes": []},
        trigger="manual",
        status="SUCCEEDED",
    )
    session.add(wf_run)
    await session.flush()
    session.add_all(
        [
            WorkflowStep(
                run_id=wf_run.id,
                sequence=1,
                node_id="ask",
                node_type="approval",
                status="SUCCEEDED",
            ),
            WorkflowApproval(
                run_id=wf_run.id,
                node_id="ask",
                message="Send it?",
                status="APPROVED",
                decided_by=owner.id,
            ),
        ]
    )
    await session.flush()
    return {
        "model": model,
        "shared": shared,
        "alias": alias,
        "elsewhere": elsewhere,
        "run": run,
        "wf_run": wf_run,
    }


async def test_history_models_and_aliases_come_back(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    extra = await add_history(session, made, superuser)
    staged = await take_backup(session, made["team"].id)

    # Since the backup: the run went on and finished (and must stay finished), the
    # workflow run, its approval and the team's model and alias were deleted, and the
    # shared model one alias pointed at was deleted.
    await session.execute(
        update(AgentRun).where(AgentRun.id == extra["run"].id).values(state=RunState.COMPLETED)
    )
    await session.execute(delete(WorkflowRun).where(WorkflowRun.id == extra["wf_run"].id))
    await session.execute(delete(ModelAlias).where(ModelAlias.team_id == made["team"].id))
    await session.execute(
        delete(Model).where(Model.id.in_([extra["model"].id, extra["shared"].id]))
    )
    await session.flush()

    await apply(session, made["team"].id, staged)

    run = await session.get(AgentRun, extra["run"].id)
    assert run.state == RunState.COMPLETED  # history is added to, never rewound
    wf_run = await session.get(WorkflowRun, extra["wf_run"].id)
    assert wf_run is not None and wf_run.status == "SUCCEEDED"
    approval = (
        await session.execute(select(WorkflowApproval).where(WorkflowApproval.run_id == wf_run.id))
    ).scalar_one()
    assert approval.decided_by == superuser.id and approval.status == "APPROVED"
    steps = (
        await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == wf_run.id))
    ).all()
    assert len(steps) == 1
    model = await session.get(Model, extra["model"].id)
    assert model is not None and model.team_id == made["team"].id
    assert await session.get(ModelAlias, extra["alias"].id) is not None
    # The alias of a shared model deleted since is left out rather than refusing.
    assert await session.get(ModelAlias, extra["elsewhere"].id) is None


async def test_a_run_whose_user_is_gone_keeps_its_record(session: AsyncSession, superuser):
    made = await make_team(session, superuser)
    extra = await add_history(session, made, superuser)
    staged = await take_backup(session, made["team"].id)
    await session.execute(delete(AgentRun).where(AgentRun.id == extra["run"].id))
    # As if the person who ran it had been deleted since.
    await session.execute(
        text("UPDATE stage_agent_runs SET user_id = :gone WHERE id = :run"),
        {"gone": uuid.uuid4(), "run": extra["run"].id},
    )
    await session.flush()

    await apply(session, made["team"].id, staged)

    run = await session.get(AgentRun, extra["run"].id)
    assert run is not None and run.user_id is None
    events = (
        await session.execute(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id))
    ).all()
    executions = (
        (await session.execute(select(ToolExecution).where(ToolExecution.run_id == run.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1 and executions[0].approval_state == ApprovalState.APPROVED
