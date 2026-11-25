"""SQL for chargeback reports (E9).

Everything here reads data the platform already records: ``usage_records`` for tokens,
``gpu_allocations`` for GPU time, ``cost_rates`` for prices. Pricing (multiplying by a
rate) happens in the service, in ``Decimal``; this module only aggregates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agents import Agent, AgentRun, Tool, ToolExecution
from app.models.auth import User
from app.models.chargeback import (
    DEFAULT_RATE_MODEL,
    DEFAULT_TOOL_RATE,
    TOOL_RATE_PREFIX,
    UNIT_BY_ENDPOINT,
    UNIT_TOKENS,
    CostBudget,
    CostRate,
)
from app.models.infrastructure import GpuAllocation
from app.models.models_registry import ApiClient, ApiKey, Model, UsageRecord
from app.models.teams import Team
from app.models.workflows import StepStatus, Workflow, WorkflowRun, WorkflowStep
from app.repositories.base import BaseRepository
from app.repositories.models_registry import timing_columns, timing_values

#: Report dimensions, and how each names its rows.
GROUPS = ("team", "api_key", "user", "end_user", "model")

#: The prefix ``Scheduler.reserve`` gives a model deployment's GPU reservation.
MODEL_PURPOSE_PREFIX = "model:"


class CostRateRepository(BaseRepository[CostRate]):
    model = CostRate

    async def list_all(self) -> Sequence[CostRate]:
        stmt = select(CostRate).order_by(
            (CostRate.model == DEFAULT_RATE_MODEL).desc(),
            CostRate.model,
            CostRate.effective_from.desc(),
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def get_exact(self, model: str, effective_from: dt.datetime) -> CostRate | None:
        stmt = select(CostRate).where(
            CostRate.model == model, CostRate.effective_from == effective_from
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def model_is_known(self, name: str) -> bool:
        """A registered model, or one the gateway has recorded usage for (a model since
        deleted still has usage worth pricing)."""
        registered = select(Model.id).where(Model.name == name).limit(1)
        if (await self.session.execute(registered)).first() is not None:
            return True
        used = select(UsageRecord.id).where(UsageRecord.model == name).limit(1)
        return (await self.session.execute(used)).first() is not None

    async def tool_is_known(self, name: str) -> bool:
        """A registered tool, or one an agent has called (E67)."""
        registered = select(Tool.id).where(Tool.name == name).limit(1)
        if (await self.session.execute(registered)).first() is not None:
            return True
        called = select(ToolExecution.id).where(ToolExecution.tool_name == name).limit(1)
        return (await self.session.execute(called)).first() is not None


class CostBudgetRepository(BaseRepository[CostBudget]):
    model = CostBudget

    async def list_all(self, *, enabled_only: bool = False) -> Sequence[CostBudget]:
        stmt = select(CostBudget).order_by(CostBudget.scope, CostBudget.subject)
        if enabled_only:
            stmt = stmt.where(CostBudget.enabled.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_subject(self, scope: str, subject: str) -> CostBudget | None:
        stmt = select(CostBudget).where(CostBudget.scope == scope, CostBudget.subject == subject)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ChargebackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def usage(
        self, *, group: str, start: dt.datetime, end: dt.datetime, where: Any = None
    ) -> list[dict[str, Any]]:
        """Usage in ``[start, end)`` by ``group``, model and the rate in force.

        The rate is chosen per call, in the database: the latest rate for the call's model
        that had started by then, else the latest default (``'*'``). A model's own rate
        always beats the default, even an older one. ``rate_id`` is null when no rate
        applies, and those tokens are reported as unpriced, never as free.
        """
        rate_id = (
            select(CostRate.id)
            .where(
                or_(CostRate.model == UsageRecord.model, CostRate.model == DEFAULT_RATE_MODEL),
                CostRate.effective_from <= UsageRecord.recorded_at,
            )
            .order_by((CostRate.model == DEFAULT_RATE_MODEL).asc(), CostRate.effective_from.desc())
            .limit(1)
            .correlate(UsageRecord)
            .scalar_subquery()
        )
        # E67: what the row counts, so speech is priced by its unit and never as tokens.
        unit = case(
            *(
                (UsageRecord.endpoint == endpoint, literal(u))
                for endpoint, u in UNIT_BY_ENDPOINT.items()
            ),
            else_=literal(UNIT_TOKENS),
        )
        calls = select(
            UsageRecord.team_id,
            UsageRecord.api_key_id,
            UsageRecord.user_id,
            UsageRecord.end_user,
            UsageRecord.end_user_trusted,
            UsageRecord.model,
            unit.label("unit"),
            UsageRecord.prompt_tokens,
            UsageRecord.completion_tokens,
            UsageRecord.client_disconnected,
            rate_id.label("rate_id"),
        ).where(UsageRecord.recorded_at >= start, UsageRecord.recorded_at < end)
        if where is not None:
            calls = calls.where(where)
        c = calls.subquery("calls")

        key_cols: list[Any]
        label_cols: list[Any]
        joins: list[tuple[Any, Any]] = []
        if group == "team":
            key_cols = [c.c.team_id]
            label_cols = [Team.name.label("label"), Team.slug.label("detail")]
            joins = [(Team, Team.id == c.c.team_id)]
        elif group == "api_key":
            key_cols = [c.c.api_key_id]
            label_cols = [ApiKey.name.label("label"), ApiClient.name.label("detail"), ApiKey.prefix]
            joins = [
                (ApiKey, ApiKey.id == c.c.api_key_id),
                (ApiClient, ApiClient.id == ApiKey.client_id),
            ]
        elif group == "user":
            key_cols = [c.c.user_id]
            label_cols = [User.username.label("label"), User.full_name.label("detail")]
            joins = [(User, User.id == c.c.user_id)]
        elif group == "end_user":
            key_cols = [c.c.end_user, c.c.end_user_trusted]
            label_cols = []
        elif group == "model":
            key_cols = []
            label_cols = []
        else:  # pragma: no cover - the API validates the group
            raise ValueError(group)

        stmt = select(
            *key_cols,
            *label_cols,
            c.c.model,
            c.c.unit,
            c.c.rate_id,
            func.count().label("requests"),
            func.coalesce(func.sum(c.c.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(c.c.completion_tokens), 0).label("completion_tokens"),
            func.count().filter(c.c.client_disconnected.is_(true())).label("partial"),
        ).select_from(c)
        for target, condition in joins:
            stmt = stmt.outerjoin(target, condition)
        stmt = stmt.group_by(*key_cols, *label_cols, c.c.model, c.c.unit, c.c.rate_id)
        return [dict(row._mapping) for row in (await self.session.execute(stmt)).all()]

    async def tool_calls(
        self,
        *,
        group: str,
        start: dt.datetime,
        end: dt.datetime,
        where: Any = None,
        workflow_where: Any = None,
    ) -> list[dict[str, Any]]:
        """Agent tool calls made in ``[start, end)`` by ``group``, tool and rate (E67).

        A call is one that ran (``success`` set), approved or not needed approval;
        refused and pending ones cost nothing. It belongs to the agent's team and the
        run's user, and (E92) the API key and end user of an `agent:` call; runs started
        otherwise, and workflow steps, are on those groupings' "none" row. ``where``
        filters on ``Agent.team_id``.

        The rate is chosen per call like :meth:`usage`: the tool's own latest rate that
        had started, else the latest ``tool:*``. Never the token default ``*``.
        """
        name = func.concat(TOOL_RATE_PREFIX, ToolExecution.tool_name)
        rate_id = (
            select(CostRate.id)
            .where(
                or_(CostRate.model == name, CostRate.model == DEFAULT_TOOL_RATE),
                CostRate.effective_from <= ToolExecution.requested_at,
            )
            .order_by((CostRate.model == DEFAULT_TOOL_RATE).asc(), CostRate.effective_from.desc())
            .limit(1)
            .correlate(ToolExecution)
            .scalar_subquery()
        )
        calls = (
            select(
                Agent.team_id.label("team_id"),
                AgentRun.user_id.label("user_id"),
                AgentRun.api_key_id.label("api_key_id"),
                AgentRun.end_user.label("end_user"),
                AgentRun.end_user_trusted.label("end_user_trusted"),
                name.label("model"),
                rate_id.label("rate_id"),
                literal(1).label("calls"),
            )
            .select_from(ToolExecution)
            .join(AgentRun, AgentRun.id == ToolExecution.run_id)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .where(
                ToolExecution.success.isnot(None),
                ToolExecution.requested_at >= start,
                ToolExecution.requested_at < end,
            )
        )
        if where is not None:
            calls = calls.where(where)
        # E86: workflow tool steps that ran, on the workflow's team and whoever started
        # the run. The step's input names the tool by id; the rate goes by its name.
        # ``workflow_where`` filters on ``Workflow.team_id``.
        step_tool = func.coalesce(Tool.name, "?")
        step_name = func.concat(TOOL_RATE_PREFIX, step_tool)
        step_rate = (
            select(CostRate.id)
            .where(
                or_(CostRate.model == step_name, CostRate.model == DEFAULT_TOOL_RATE),
                CostRate.effective_from <= WorkflowStep.started_at,
            )
            .order_by((CostRate.model == DEFAULT_TOOL_RATE).asc(), CostRate.effective_from.desc())
            .limit(1)
            .correlate(WorkflowStep, Tool)
            .scalar_subquery()
        )
        # E92: a failed step is charged for each attempt that reached the tool.
        failed_calls = cast(WorkflowStep.output["tool_calls"].astext, Integer)
        steps = (
            select(
                Workflow.team_id.label("team_id"),
                WorkflowRun.started_by.label("user_id"),
                literal(None, PgUUID(as_uuid=True)).label("api_key_id"),
                literal(None, String).label("end_user"),
                literal(False).label("end_user_trusted"),
                step_name.label("model"),
                step_rate.label("rate_id"),
                # E96: a step that succeeded after earlier attempts reached the tool
                # records them too; otherwise it is one call.
                func.coalesce(failed_calls, literal(1)).label("calls"),
            )
            .select_from(WorkflowStep)
            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .outerjoin(Tool, cast(Tool.id, String) == WorkflowStep.input["tool_id"].astext)
            .where(
                WorkflowStep.node_type == "tool",
                or_(
                    and_(
                        WorkflowStep.status == StepStatus.SUCCEEDED,
                        # A step that only asked for an approval ran nothing.
                        or_(
                            WorkflowStep.output.is_(None),
                            ~WorkflowStep.output.has_key("awaiting"),
                        ),
                    ),
                    and_(
                        WorkflowStep.status == StepStatus.FAILED,
                        WorkflowStep.output.has_key("tool_calls"),
                    ),
                ),
                WorkflowStep.started_at >= start,
                WorkflowStep.started_at < end,
            )
        )
        if workflow_where is not None:
            steps = steps.where(workflow_where)
        c = calls.union_all(steps).subquery("tool_calls")

        key_cols: list[Any] = []
        label_cols: list[Any] = []
        joins: list[tuple[Any, Any]] = []
        if group == "team":
            key_cols = [c.c.team_id]
            label_cols = [Team.name.label("label"), Team.slug.label("detail")]
            joins = [(Team, Team.id == c.c.team_id)]
        elif group == "user":
            key_cols = [c.c.user_id]
            label_cols = [User.username.label("label"), User.full_name.label("detail")]
            joins = [(User, User.id == c.c.user_id)]
        elif group == "api_key":
            key_cols = [c.c.api_key_id]
            label_cols = [ApiKey.name.label("label"), ApiClient.name.label("detail"), ApiKey.prefix]
            joins = [
                (ApiKey, ApiKey.id == c.c.api_key_id),
                (ApiClient, ApiClient.id == ApiKey.client_id),
            ]
        elif group == "end_user":
            key_cols = [c.c.end_user, c.c.end_user_trusted]
        stmt = select(
            *key_cols,
            *label_cols,
            c.c.model,
            c.c.rate_id,
            func.coalesce(func.sum(c.c.calls), 0).label("calls"),
        ).select_from(c)
        for target, condition in joins:
            stmt = stmt.outerjoin(target, condition)
        stmt = stmt.group_by(*key_cols, *label_cols, c.c.model, c.c.rate_id)
        out = []
        for row in (await self.session.execute(stmt)).all():
            values = dict(row._mapping)
            # The keys a usage row has, so the report files both on the same lines.
            values.setdefault("team_id", None)
            values.setdefault("user_id", None)
            values.setdefault("api_key_id", None)
            values.setdefault("end_user", None)
            values.setdefault("end_user_trusted", False)
            out.append(values)
        return out

    async def timings(
        self, *, group: str | None, start: dt.datetime, end: dt.datetime, where: Any = None
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        """Latency and streaming timings in ``[start, end)`` per ``group`` (E40).

        Keyed like the report's lines: ``(model,)``, ``(end_user, trusted)`` or
        ``(<group>_id,)``; ``group=None`` gives one entry, ``()``, over everything. A
        separate query from :meth:`usage`, which also splits by model and rate: a
        percentile of those parts would not be the group's percentile.
        """
        key_cols: list[Any]
        if group is None:
            key_cols = []
        elif group == "model":
            key_cols = [UsageRecord.model]
        elif group == "end_user":
            key_cols = [UsageRecord.end_user, UsageRecord.end_user_trusted]
        elif group in {"team", "api_key", "user"}:
            key_cols = [getattr(UsageRecord, f"{group}_id")]
        else:  # pragma: no cover - the API validates the group
            raise ValueError(group)
        stmt = select(
            *key_cols,
            func.avg(UsageRecord.latency_ms).label("avg_latency_ms"),
            # E67: per model, only the model's own times to first output.
            *timing_columns(direct_only=group == "model"),
        ).where(UsageRecord.recorded_at >= start, UsageRecord.recorded_at < end)
        if where is not None:
            stmt = stmt.where(where)
        if key_cols:
            stmt = stmt.group_by(*key_cols)
        out: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in (await self.session.execute(stmt)).all():
            values = timing_values(row)
            values["avg_latency_ms"] = (
                None if row.avg_latency_ms is None else round(float(row.avg_latency_ms), 2)
            )
            out[tuple(row[: len(key_cols)])] = values
        return out

    async def tokens_by_model(self, *, start: dt.datetime, end: dt.datetime) -> dict[str, int]:
        """Every caller's tokens per model, unscoped: the denominator GPU time is shared
        out by. A team-scoped report must not hand its team the whole GPU bill just
        because it cannot see the other teams' traffic."""
        stmt = (
            select(
                UsageRecord.model,
                func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens),
            )
            .where(UsageRecord.recorded_at >= start, UsageRecord.recorded_at < end)
            .group_by(UsageRecord.model)
        )
        return {model: int(total or 0) for model, total in (await self.session.execute(stmt)).all()}

    async def model_gpu_reservations(
        self, *, start: dt.datetime, end: dt.datetime
    ) -> list[dict[str, Any]]:
        """GPU reservations held by model deployments that overlap ``[start, end)``,
        with the model's name. One row per GPU; open reservations have no
        ``released_at``. Reservations for anything but a model are not chargeable to a
        model's callers and are left out."""
        model_id = func.substr(GpuAllocation.purpose, len(MODEL_PURPOSE_PREFIX) + 1)
        stmt = (
            select(
                Model.name.label("model"),
                GpuAllocation.reserved_at,
                GpuAllocation.released_at,
            )
            .join(Model, cast(Model.id, String) == model_id)
            .where(
                GpuAllocation.purpose.startswith(MODEL_PURPOSE_PREFIX),
                GpuAllocation.reserved_at < end,
                or_(GpuAllocation.released_at.is_(None), GpuAllocation.released_at > start),
            )
        )
        return [dict(row._mapping) for row in (await self.session.execute(stmt)).all()]


__all__ = ["GROUPS", "ChargebackRepository", "CostRateRepository"]
