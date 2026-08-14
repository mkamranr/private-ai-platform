"""Repositories for the model registry, deployments, aliases and usage (M07-M09)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from app.models.models_registry import (
    ACTIVE_STATES,
    ApiClient,
    ApiKey,
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelFile,
    UsageRecord,
)
from app.repositories.base import BaseRepository


class ModelRepository(BaseRepository[Model]):
    model = Model

    async def get_by_name(self, name: str) -> Model | None:
        stmt = select(Model).where(Model.name == name).options(selectinload(Model.files))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_files(self, model_id: uuid.UUID) -> Model | None:
        stmt = select(Model).where(Model.id == model_id).options(selectinload(Model.files))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_models(
        self, *, model_type: str | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[Model]:
        stmt = select(Model).options(selectinload(Model.files))
        if model_type:
            stmt = stmt.where(Model.type == model_type)
        stmt = stmt.order_by(Model.name).limit(limit).offset(offset)
        return (await self.session.execute(stmt)).scalars().all()

    async def replace_files(self, model_id: uuid.UUID, files: Sequence[ModelFile]) -> None:
        """Swap in a freshly scanned file list.

        Re-importing must converge, not accumulate: a model re-scanned after its shards
        were repacked should reflect what is on disk now, not the union of every scan.
        """
        await self.session.execute(delete(ModelFile).where(ModelFile.model_id == model_id))
        for file in files:
            self.session.add(file)


class ModelDeploymentRepository(BaseRepository[ModelDeployment]):
    model = ModelDeployment

    async def get_with_model(self, deployment_id: uuid.UUID) -> ModelDeployment | None:
        stmt = (
            select(ModelDeployment)
            .where(ModelDeployment.id == deployment_id)
            .options(selectinload(ModelDeployment.model))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_deployments(
        self, *, model_id: uuid.UUID | None = None, states: Sequence[str] | None = None
    ) -> Sequence[ModelDeployment]:
        stmt = select(ModelDeployment).options(selectinload(ModelDeployment.model))
        if model_id is not None:
            stmt = stmt.where(ModelDeployment.model_id == model_id)
        if states:
            stmt = stmt.where(ModelDeployment.state.in_(states))
        stmt = stmt.order_by(ModelDeployment.created_at.desc())
        return (await self.session.execute(stmt)).scalars().all()

    async def list_pending(self) -> Sequence[ModelDeployment]:
        """Deployments the worker still has to drive.

        Includes REQUESTED through HEALTH_CHECK. On restart this is what lets a
        deployment that was mid-flight resume rather than sit in SCHEDULING forever —
        the state machine lives in the database, not in the worker's memory.
        """
        stmt = (
            select(ModelDeployment)
            .where(
                ModelDeployment.state.in_(
                    [
                        DeploymentState.REQUESTED,
                        DeploymentState.VALIDATING,
                        DeploymentState.SCHEDULING,
                        DeploymentState.CREATING,
                        DeploymentState.STARTING,
                        DeploymentState.HEALTH_CHECK,
                    ]
                )
            )
            .options(selectinload(ModelDeployment.model))
            .order_by(ModelDeployment.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def serving_for_model(self, model_id: uuid.UUID) -> Sequence[ModelDeployment]:
        """RUNNING deployments for a model, oldest first.

        Order matters: V1 alias resolution is deterministic — the first healthy
        deployment by creation order answers — so that repeated calls hit the same
        instance and a debugging session is reproducible. Round-robin and failover are
        V2 and slot in behind the same query.
        """
        stmt = (
            select(ModelDeployment)
            .where(
                ModelDeployment.model_id == model_id,
                ModelDeployment.state == DeploymentState.RUNNING,
            )
            .order_by(ModelDeployment.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def active_for_model(self, model_id: uuid.UUID) -> Sequence[ModelDeployment]:
        stmt = select(ModelDeployment).where(
            ModelDeployment.model_id == model_id,
            ModelDeployment.state.in_(list(ACTIVE_STATES)),
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_all(self) -> int:
        return int(
            (
                await self.session.execute(select(func.count()).select_from(ModelDeployment))
            ).scalar_one()
        )


class ModelAliasRepository(BaseRepository[ModelAlias]):
    model = ModelAlias

    async def get_by_alias(self, alias: str) -> ModelAlias | None:
        stmt = (
            select(ModelAlias)
            .where(ModelAlias.alias == alias, ModelAlias.enabled.is_(True))
            .options(selectinload(ModelAlias.model))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> Sequence[ModelAlias]:
        stmt = select(ModelAlias).options(selectinload(ModelAlias.model)).order_by(ModelAlias.alias)
        return (await self.session.execute(stmt)).scalars().all()

    async def for_model(self, model_id: uuid.UUID) -> Sequence[ModelAlias]:
        stmt = select(ModelAlias).where(ModelAlias.model_id == model_id)
        return (await self.session.execute(stmt)).scalars().all()


class ApiKeyRepository(BaseRepository[ApiKey]):
    model = ApiKey

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Look a key up by its hash — the gateway's hot path.

        Backed by a unique index. This runs on every inference request, which is why
        keys are SHA-256 and not argon2.
        """
        stmt = (
            select(ApiKey).where(ApiKey.key_hash == key_hash).options(selectinload(ApiKey.client))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_client(self, client_id: uuid.UUID) -> Sequence[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.client_id == client_id).order_by(ApiKey.created_at)
        return (await self.session.execute(stmt)).scalars().all()

    async def list_all(self) -> Sequence[ApiKey]:
        stmt = select(ApiKey).options(selectinload(ApiKey.client)).order_by(ApiKey.created_at)
        return (await self.session.execute(stmt)).scalars().all()


class ApiClientRepository(BaseRepository[ApiClient]):
    model = ApiClient

    async def get_by_name(self, name: str) -> ApiClient | None:
        stmt = select(ApiClient).where(ApiClient.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> Sequence[ApiClient]:
        stmt = select(ApiClient).options(selectinload(ApiClient.keys)).order_by(ApiClient.name)
        return (await self.session.execute(stmt)).scalars().all()


class UsageRepository(BaseRepository[UsageRecord]):
    model = UsageRecord

    async def summary(
        self, *, since: dt.datetime, api_key_id: uuid.UUID | None = None
    ) -> list[dict]:
        """Aggregate usage by model.

        Aggregated in the database, not in Python: a month of gateway traffic is far too
        many rows to pull back and fold client-side.
        """
        stmt = (
            select(
                UsageRecord.model,
                func.count().label("requests"),
                func.sum(UsageRecord.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageRecord.completion_tokens).label("completion_tokens"),
                func.avg(UsageRecord.latency_ms).label("avg_latency_ms"),
            )
            .where(UsageRecord.recorded_at >= since)
            .group_by(UsageRecord.model)
            .order_by(func.count().desc())
        )
        if api_key_id is not None:
            stmt = stmt.where(UsageRecord.api_key_id == api_key_id)

        return [
            {
                "model": row.model,
                "requests": row.requests,
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "avg_latency_ms": round(float(row.avg_latency_ms or 0), 2),
            }
            for row in (await self.session.execute(stmt)).all()
        ]

    async def by_end_user(self, *, since: dt.datetime, limit: int = 100) -> list[dict]:
        """Aggregate usage per end user, behind a shared frontend (M17).

        Grouped by trustworthiness as well as by name, so the two never merge into one
        row. A self-reported `user` string and an identity forwarded by a frontend the
        platform authenticates are different claims, and a chargeback report that added
        them together would bill someone for traffic anyone could have labelled as theirs.
        """
        stmt = (
            select(
                UsageRecord.end_user,
                UsageRecord.end_user_trusted,
                func.count().label("requests"),
                func.sum(UsageRecord.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageRecord.completion_tokens).label("completion_tokens"),
                func.max(UsageRecord.recorded_at).label("last_seen_at"),
            )
            .where(UsageRecord.recorded_at >= since, UsageRecord.end_user.isnot(None))
            .group_by(UsageRecord.end_user, UsageRecord.end_user_trusted)
            .order_by(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens).desc())
            .limit(limit)
        )
        return [
            {
                "end_user": row.end_user,
                "trusted": row.end_user_trusted,
                "requests": row.requests,
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "last_seen_at": row.last_seen_at,
            }
            for row in (await self.session.execute(stmt)).all()
        ]

    async def hourly_series(self, *, since: dt.datetime) -> list[dict]:
        """Requests and tokens per hour, for the dashboard chart (M21).

        Bucketed by the database. Returning raw records for the browser to bucket would
        mean shipping a day of gateway traffic to render one sparkline.
        """
        bucket = func.date_trunc("hour", UsageRecord.recorded_at).label("hour")
        stmt = (
            select(
                bucket,
                func.count().label("requests"),
                func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens).label("tokens"),
            )
            .where(UsageRecord.recorded_at >= since)
            .group_by(bucket)
            .order_by(bucket)
        )
        return [
            {"hour": row.hour, "requests": row.requests, "tokens": int(row.tokens or 0)}
            for row in (await self.session.execute(stmt)).all()
        ]

    async def recent(self, *, limit: int = 50) -> Sequence[UsageRecord]:
        stmt = select(UsageRecord).order_by(UsageRecord.recorded_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()
