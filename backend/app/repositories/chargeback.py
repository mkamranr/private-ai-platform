"""SQL for chargeback reports (E9).

Everything here reads data the platform already records: ``usage_records`` for tokens,
``gpu_allocations`` for GPU time, ``cost_rates`` for prices. Pricing (multiplying by a
rate) happens in the service, in ``Decimal``; this module only aggregates.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, cast, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import User
from app.models.chargeback import DEFAULT_RATE_MODEL, CostBudget, CostRate
from app.models.infrastructure import GpuAllocation
from app.models.models_registry import ApiClient, ApiKey, Model, UsageRecord
from app.models.teams import Team
from app.repositories.base import BaseRepository

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
        calls = select(
            UsageRecord.team_id,
            UsageRecord.api_key_id,
            UsageRecord.user_id,
            UsageRecord.end_user,
            UsageRecord.end_user_trusted,
            UsageRecord.model,
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
            c.c.rate_id,
            func.count().label("requests"),
            func.coalesce(func.sum(c.c.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(c.c.completion_tokens), 0).label("completion_tokens"),
            func.count().filter(c.c.client_disconnected.is_(true())).label("partial"),
        ).select_from(c)
        for target, condition in joins:
            stmt = stmt.outerjoin(target, condition)
        stmt = stmt.group_by(*key_cols, *label_cols, c.c.model, c.c.rate_id)
        return [dict(row._mapping) for row in (await self.session.execute(stmt)).all()]

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
