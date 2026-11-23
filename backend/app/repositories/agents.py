"""Repositories for agents, skills, tools, MCP servers and runs (M10-M14)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.models.agents import (
    Agent,
    AgentAnswerScore,
    AgentRun,
    AgentRunEvent,
    AgentSkill,
    AgentTool,
    AgentVersion,
    AnswerScoreStatus,
    ApprovalState,
    McpServer,
    RunState,
    Skill,
    Tool,
    ToolExecution,
)
from app.repositories.base import BaseRepository


class ToolRepository(BaseRepository[Tool]):
    model = Tool

    async def get_by_name(self, name: str) -> Tool | None:
        return (
            await self.session.execute(select(Tool).where(Tool.name == name))
        ).scalar_one_or_none()

    async def list_all(self, *, enabled_only: bool = False) -> Sequence[Tool]:
        stmt = select(Tool).order_by(Tool.name)
        if enabled_only:
            stmt = stmt.where(Tool.enabled.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def list_for_server(self, server_id: uuid.UUID) -> Sequence[Tool]:
        stmt = select(Tool).where(Tool.mcp_server_id == server_id).order_by(Tool.name)
        return (await self.session.execute(stmt)).scalars().all()


class McpServerRepository(BaseRepository[McpServer]):
    model = McpServer

    async def get_by_name(self, name: str) -> McpServer | None:
        return (
            await self.session.execute(select(McpServer).where(McpServer.name == name))
        ).scalar_one_or_none()

    async def list_all(self) -> Sequence[McpServer]:
        return (
            (await self.session.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
        )


class SkillRepository(BaseRepository[Skill]):
    model = Skill

    async def get_by_name(self, name: str) -> Skill | None:
        return (
            await self.session.execute(select(Skill).where(Skill.name == name))
        ).scalar_one_or_none()

    async def list_all(self) -> Sequence[Skill]:
        return (await self.session.execute(select(Skill).order_by(Skill.name))).scalars().all()


class AgentRepository(BaseRepository[Agent]):
    model = Agent

    async def get_by_slug(self, slug: str) -> Agent | None:
        return (
            await self.session.execute(select(Agent).where(Agent.slug == slug))
        ).scalar_one_or_none()

    async def list_all(self, *, enabled_only: bool = False) -> Sequence[Agent]:
        stmt = select(Agent).order_by(Agent.slug)
        if enabled_only:
            stmt = stmt.where(Agent.enabled.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return (await self.session.execute(select(func.count()).select_from(Agent))).scalar_one()


class AgentVersionRepository(BaseRepository[AgentVersion]):
    model = AgentVersion

    async def get_resolved(self, version_id: uuid.UUID) -> AgentVersion | None:
        """A version with its tools and skills eagerly loaded.

        Eager because assembling an AgentSpec touches all of them, and a run that
        lazy-loads mid-execution would hit the session from inside the runtime — which is
        precisely the boundary the interface exists to keep clean.
        """
        stmt = (
            select(AgentVersion)
            .where(AgentVersion.id == version_id)
            .options(
                selectinload(AgentVersion.tools).selectinload(AgentTool.tool),
                selectinload(AgentVersion.skills).selectinload(AgentSkill.skill),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_current(self, agent_id: uuid.UUID) -> AgentVersion | None:
        stmt = (
            select(AgentVersion)
            .join(Agent, Agent.id == AgentVersion.agent_id)
            .where(
                AgentVersion.agent_id == agent_id,
                AgentVersion.version == Agent.current_version,
            )
            .options(
                selectinload(AgentVersion.tools).selectinload(AgentTool.tool),
                selectinload(AgentVersion.skills).selectinload(AgentSkill.skill),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_agent(self, agent_id: uuid.UUID) -> Sequence[AgentVersion]:
        stmt = (
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def next_version(self, agent_id: uuid.UUID) -> int:
        current = (
            await self.session.execute(
                select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        return int(current or 0) + 1


class AgentRunRepository(BaseRepository[AgentRun]):
    model = AgentRun

    async def get_with_version(self, run_id: uuid.UUID) -> AgentRun | None:
        stmt = (
            select(AgentRun)
            .where(AgentRun.id == run_id)
            .options(
                selectinload(AgentRun.agent_version).selectinload(AgentVersion.tools),
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_trace_id(self, trace_id: str) -> AgentRun | None:
        """The run behind a W3C trace id (M19).

        Eager-loads the agent, because the caller arrived holding an opaque id and the
        first thing they need is which agent it was — a lazy load in the response
        serialiser would raise outside the session.
        """
        stmt = (
            select(AgentRun)
            .where(AgentRun.trace_id == trace_id)
            .options(selectinload(AgentRun.agent))
            # A trace id is unique in practice, but this is an index, not a constraint:
            # nothing stops two runs being written with the same id by a caller that
            # propagated one inwards. Newest wins rather than the query raising.
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_agent(self, agent_id: uuid.UUID, *, limit: int = 50) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.agent_id == agent_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_waiting(self, *, limit: int = 100) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.state == RunState.WAITING_FOR_APPROVAL)
            .order_by(AgentRun.created_at)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_by_state(self) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(AgentRun.state, func.count()).group_by(AgentRun.state)
            )
        ).all()
        return {row[0]: row[1] for row in rows}


class AgentRunEventRepository(BaseRepository[AgentRunEvent]):
    model = AgentRunEvent

    async def list_for_run(
        self, run_id: uuid.UUID, *, after_sequence: int = 0
    ) -> Sequence[AgentRunEvent]:
        stmt = (
            select(AgentRunEvent)
            .where(AgentRunEvent.run_id == run_id, AgentRunEvent.sequence > after_sequence)
            .order_by(AgentRunEvent.sequence)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def next_sequence(self, run_id: uuid.UUID) -> int:
        current = (
            await self.session.execute(
                select(func.max(AgentRunEvent.sequence)).where(AgentRunEvent.run_id == run_id)
            )
        ).scalar_one_or_none()
        return int(current or 0) + 1


class ToolExecutionRepository(BaseRepository[ToolExecution]):
    model = ToolExecution

    async def list_for_run(self, run_id: uuid.UUID) -> Sequence[ToolExecution]:
        stmt = (
            select(ToolExecution)
            .where(ToolExecution.run_id == run_id)
            .order_by(ToolExecution.requested_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def pending_for_run(self, run_id: uuid.UUID) -> ToolExecution | None:
        """The call this run is suspended on.

        At most one: the runtime stops at the first call needing approval rather than
        queuing several, so an approver is never asked to reason about a batch whose
        later members depend on the earlier ones' results.
        """
        stmt = (
            select(ToolExecution)
            .where(
                ToolExecution.run_id == run_id,
                ToolExecution.approval_state == ApprovalState.PENDING,
            )
            .order_by(ToolExecution.requested_at)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_pending(self, *, limit: int = 100) -> Sequence[ToolExecution]:
        stmt = (
            select(ToolExecution)
            .where(ToolExecution.approval_state == ApprovalState.PENDING)
            .order_by(ToolExecution.requested_at)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def expire_older_than(self, cutoff: dt.datetime) -> int:
        """Mark stale approvals EXPIRED. Returns how many.

        EXPIRED rather than REJECTED: nobody decided, and an audit that recorded a
        timeout as a refusal would attribute it to a person who never saw it.
        """
        stale = (
            (
                await self.session.execute(
                    select(ToolExecution).where(
                        ToolExecution.approval_state == ApprovalState.PENDING,
                        ToolExecution.requested_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for execution in stale:
            execution.approval_state = ApprovalState.EXPIRED
            execution.decided_at = dt.datetime.now(dt.UTC)
            execution.decision_reason = "No approver responded within the timeout."
        return len(stale)


class AgentAnswerScoreRepository(BaseRepository[AgentAnswerScore]):
    """Sampled agent answers and their faithfulness (E70)."""

    model = AgentAnswerScore

    async def claim_pending(self) -> AgentAnswerScore | None:
        """The oldest answer waiting to be judged, locked for this worker."""
        stmt = (
            select(AgentAnswerScore)
            .where(AgentAnswerScore.status == AnswerScoreStatus.PENDING)
            .order_by(AgentAnswerScore.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def for_agent(self, agent_id: uuid.UUID, *, limit: int) -> Sequence[AgentAnswerScore]:
        stmt = (
            select(AgentAnswerScore)
            .where(AgentAnswerScore.agent_id == agent_id)
            .order_by(AgentAnswerScore.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def summary(self, agent_id: uuid.UUID, since: dt.datetime) -> dict[str, object]:
        """Counts by status, the mean faithfulness and the unsupported claims since then."""
        rows = (
            await self.session.execute(
                select(
                    AgentAnswerScore.status,
                    func.count(),
                    func.avg(AgentAnswerScore.faithfulness),
                    func.coalesce(
                        func.sum(AgentAnswerScore.verdict["unsupported"].as_integer()),
                        0,
                    ),
                )
                .where(
                    AgentAnswerScore.agent_id == agent_id,
                    AgentAnswerScore.created_at >= since,
                )
                .group_by(AgentAnswerScore.status)
            )
        ).all()
        counts = {str(status): int(n) for status, n, _, _ in rows}
        scored = next((r for r in rows if r[0] == AnswerScoreStatus.SCORED), None)
        return {
            "counts": counts,
            "average_faithfulness": (
                round(float(scored[2]), 4) if scored and scored[2] is not None else None
            ),
            "unsupported_claims": int(scored[3]) if scored else 0,
        }

    async def delete_older_than(self, cutoff: dt.datetime) -> int:
        result = await self.session.execute(
            delete(AgentAnswerScore).where(AgentAnswerScore.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)
