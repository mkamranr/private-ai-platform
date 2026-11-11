"""Read-only aggregates for the dashboard sections added in E8.

Each method is one or two grouped queries. Scoped resources take the caller's team
`Scope` clause, so a team member's dashboard counts only what their lists would show.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scope import Scope
from app.models.agents import Agent, AgentRun, ApprovalState, ToolExecution
from app.models.knowledge import Document, DocumentStatus, KnowledgeBase
from app.models.operations import OPEN_ALERT_STATES, Alert, InferenceBackend
from app.models.workflows import Workflow, WorkflowRun

#: Documents somewhere between upload and indexed.
IN_PROGRESS_DOCUMENT_STATES = (
    DocumentStatus.UPLOADED,
    DocumentStatus.PARSING,
    DocumentStatus.OCR,
    DocumentStatus.CHUNKING,
    DocumentStatus.EMBEDDING,
)


def _where(stmt: Any, clause: ColumnElement[bool] | None) -> Any:
    return stmt if clause is None else stmt.where(clause)


class DashboardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def alerts(self, *, recent: int = 5) -> dict[str, Any]:
        open_states = [str(s) for s in OPEN_ALERT_STATES]
        counts = (
            await self.session.execute(
                select(Alert.severity, Alert.state, func.count())
                .where(Alert.state.in_(open_states))
                .group_by(Alert.severity, Alert.state)
            )
        ).all()
        latest = (
            (
                await self.session.execute(
                    select(Alert)
                    .where(Alert.state.in_(open_states))
                    .order_by(Alert.last_seen_at.desc())
                    .limit(recent)
                )
            )
            .scalars()
            .all()
        )
        return {"counts": [(sev, state, int(n)) for sev, state, n in counts], "latest": latest}

    async def backends(self) -> dict[str, Any]:
        rows = (
            await self.session.execute(
                select(
                    InferenceBackend.status,
                    InferenceBackend.enabled,
                    func.count(),
                    func.max(InferenceBackend.last_checked_at),
                ).group_by(InferenceBackend.status, InferenceBackend.enabled)
            )
        ).all()
        return {"rows": [(status, enabled, int(n), at) for status, enabled, n, at in rows]}

    async def knowledge(self, scope: Scope, *, since: dt.datetime) -> dict[str, Any]:
        team = scope.clause(KnowledgeBase.team_id)
        bases = (
            await self.session.execute(
                _where(select(func.count()).select_from(KnowledgeBase), team)
            )
        ).scalar_one()
        doc_stmt = (
            select(
                func.count().label("documents"),
                func.count().filter(Document.status == DocumentStatus.INDEXED).label("indexed"),
                func.count()
                .filter(Document.status.in_([str(s) for s in IN_PROGRESS_DOCUMENT_STATES]))
                .label("in_progress"),
                func.count().filter(Document.status == DocumentStatus.FAILED).label("failed"),
                func.count()
                .filter(Document.status == DocumentStatus.FAILED, Document.updated_at >= since)
                .label("failed_in_window"),
                func.count().filter(Document.created_at >= since).label("added_in_window"),
            )
            .select_from(Document)
            .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
            .where(Document.is_current.is_(True))
        )
        row = (await self.session.execute(_where(doc_stmt, team))).one()
        return {"bases": int(bases), **{k: int(v or 0) for k, v in row._mapping.items()}}

    async def agents(self, scope: Scope, *, since: dt.datetime) -> dict[str, Any]:
        team = scope.clause(Agent.team_id)
        runs = (
            await self.session.execute(
                _where(
                    select(AgentRun.state, func.count())
                    .join(Agent, Agent.id == AgentRun.agent_id)
                    .where(AgentRun.created_at >= since)
                    .group_by(AgentRun.state),
                    team,
                )
            )
        ).all()
        tools = (
            await self.session.execute(
                _where(
                    select(
                        func.count().label("calls"),
                        func.count().filter(ToolExecution.success.is_(False)).label("failed"),
                    )
                    .select_from(ToolExecution)
                    .join(AgentRun, AgentRun.id == ToolExecution.run_id)
                    .join(Agent, Agent.id == AgentRun.agent_id)
                    .where(ToolExecution.requested_at >= since),
                    team,
                )
            )
        ).one()
        pending = (
            await self.session.execute(
                _where(
                    select(func.count())
                    .select_from(ToolExecution)
                    .join(AgentRun, AgentRun.id == ToolExecution.run_id)
                    .join(Agent, Agent.id == AgentRun.agent_id)
                    .where(ToolExecution.approval_state == ApprovalState.PENDING),
                    team,
                )
            )
        ).scalar_one()
        return {
            "runs": {str(state): int(n) for state, n in runs},
            "tool_calls": int(tools.calls or 0),
            "tool_calls_failed": int(tools.failed or 0),
            "approvals_pending": int(pending or 0),
        }

    async def workflows(self, scope: Scope, *, since: dt.datetime) -> dict[str, int]:
        rows = (
            await self.session.execute(
                _where(
                    select(WorkflowRun.status, func.count())
                    .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
                    .where(WorkflowRun.created_at >= since)
                    .group_by(WorkflowRun.status),
                    scope.clause(Workflow.team_id),
                )
            )
        ).all()
        return {str(status): int(n) for status, n in rows}
