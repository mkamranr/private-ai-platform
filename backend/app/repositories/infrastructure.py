"""Repositories for nodes, GPUs, metrics, allocations and containers (M04-M06)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import selectinload

from app.models.infrastructure import (
    Container,
    EnrollmentStatus,
    Gpu,
    GpuAllocation,
    GpuHealthEvent,
    GpuMetric,
    GpuProcess,
    Node,
    NodeEnrollment,
)
from app.repositories.base import BaseRepository


class NodeRepository(BaseRepository[Node]):
    model = Node

    async def get_by_name(self, name: str) -> Node | None:
        stmt = select(Node).where(Node.name == name).options(selectinload(Node.gpus))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_with_gpus(self, node_id: uuid.UUID) -> Node | None:
        stmt = select(Node).where(Node.id == node_id).options(selectinload(Node.gpus))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> Sequence[Node]:
        stmt = (
            select(Node)
            .options(selectinload(Node.gpus))
            .order_by(Node.name)
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_pollable(self) -> Sequence[Node]:
        """Nodes the background workers should contact.

        Everything except nodes an operator has explicitly disabled. Offline nodes are
        deliberately included: the poller is how a node comes *back*, so skipping them
        would make an outage permanent.

        COALESCE rather than ``isnot``: ``->>`` returns NULL when the key is absent,
        which is the common case, and any direct comparison against NULL is NULL —
        excluding every node that has no ``disabled`` label at all. (``isnot`` is also
        only valid against NULL/TRUE/FALSE and generates invalid SQL for a string.)
        """
        stmt = select(Node).where(func.coalesce(Node.labels["disabled"].astext, "false") != "true")
        return (await self.session.execute(stmt)).scalars().all()


class NodeEnrollmentRepository(BaseRepository[NodeEnrollment]):
    model = NodeEnrollment

    async def get_by_token_hash(self, token_hash: str) -> NodeEnrollment | None:
        stmt = select(NodeEnrollment).where(NodeEnrollment.token_hash == token_hash)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def pending_for_name(self, node_name: str) -> NodeEnrollment | None:
        stmt = select(NodeEnrollment).where(
            NodeEnrollment.node_name == node_name,
            NodeEnrollment.status == EnrollmentStatus.PENDING,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_filtered(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[NodeEnrollment]:
        stmt = select(NodeEnrollment).order_by(NodeEnrollment.created_at.desc())
        if status:
            stmt = stmt.where(NodeEnrollment.status == status)
        return (await self.session.execute(stmt.limit(limit).offset(offset))).scalars().all()

    async def count_filtered(self, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(NodeEnrollment)
        if status:
            stmt = stmt.where(NodeEnrollment.status == status)
        return int((await self.session.execute(stmt)).scalar_one())

    async def claim(self, token_hash: str, *, source_ip: str | None) -> bool:
        """Burn the token, atomically. True when this caller won it.

        A compare-and-set, never a read-then-write: two nodes racing the same token — or
        one node and an attacker — must not both succeed, and a check followed by an
        update leaves exactly that window. Whether the row was already consumed, expired
        or revoked is deliberately not distinguished here; the caller reports one generic
        failure so a probe cannot learn which.

        Runs in the caller's transaction, alongside the node insert, so there is no state
        in which the token is spent but no node exists.
        """
        stmt = (
            update(NodeEnrollment)
            .where(
                NodeEnrollment.token_hash == token_hash,
                NodeEnrollment.status == EnrollmentStatus.PENDING,
                NodeEnrollment.expires_at > func.now(),
            )
            .values(
                status=EnrollmentStatus.CONSUMED,
                consumed_at=func.now(),
                consumed_from_ip=source_ip,
            )
        )
        result = cast(CursorResult, await self.session.execute(stmt))
        return result.rowcount == 1

    async def expire_stale(self) -> int:
        """Flip elapsed invitations out of PENDING.

        Not cosmetic: the partial unique index only covers PENDING rows, so an expired
        one left in that state blocks re-issuing an enrolment for the same node for ever.
        """
        stmt = (
            update(NodeEnrollment)
            .where(
                NodeEnrollment.status == EnrollmentStatus.PENDING,
                NodeEnrollment.expires_at <= func.now(),
            )
            .values(status=EnrollmentStatus.EXPIRED)
        )
        return cast(CursorResult, await self.session.execute(stmt)).rowcount

    async def purge_settled(self, older_than: dt.datetime) -> int:
        """Delete finished enrolments. Consumed rows are kept a while on purpose —
        "which invitation produced this node, from what address?" is an incident question.
        """
        stmt = delete(NodeEnrollment).where(
            NodeEnrollment.status != EnrollmentStatus.PENDING,
            NodeEnrollment.updated_at < older_than,
        )
        return cast(CursorResult, await self.session.execute(stmt)).rowcount


class GpuRepository(BaseRepository[Gpu]):
    model = Gpu

    async def get_by_uuid(self, gpu_uuid: str) -> Gpu | None:
        stmt = select(Gpu).where(Gpu.uuid == gpu_uuid)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_node(self, node_id: uuid.UUID) -> Sequence[Gpu]:
        stmt = select(Gpu).where(Gpu.node_id == node_id).order_by(Gpu.index)
        return (await self.session.execute(stmt)).scalars().all()

    async def list_all(self) -> Sequence[Gpu]:
        stmt = select(Gpu).order_by(Gpu.node_id, Gpu.index)
        return (await self.session.execute(stmt)).scalars().all()

    async def capacity_summary(self) -> dict[str, Any]:
        """Fleet-wide GPU capacity, aggregated in the database.

        Two queries rather than pulling every GPU and its latest sample to the client:
        at four GPUs a node and a hundred nodes that is 400 rows plus 400 samples to
        fold, on a screen that refreshes every few seconds.
        """
        latest = (
            select(GpuMetric)
            .order_by(GpuMetric.gpu_id, GpuMetric.recorded_at.desc())
            .distinct(GpuMetric.gpu_id)
            .subquery()
        )
        row = (
            await self.session.execute(
                select(
                    func.count().label("total"),
                    func.coalesce(func.avg(latest.c.utilization_percent), 0.0).label("util"),
                    func.coalesce(func.sum(latest.c.memory_used_mib), 0).label("used"),
                    func.coalesce(func.sum(latest.c.memory_total_mib), 0).label("capacity"),
                ).select_from(Gpu.__table__.outerjoin(latest, latest.c.gpu_id == Gpu.id))
            )
        ).one()

        allocated = (
            await self.session.execute(
                select(func.count()).select_from(
                    select(GpuAllocation.node_id, GpuAllocation.gpu_index)
                    .where(GpuAllocation.released_at.is_(None))
                    .subquery()
                )
            )
        ).scalar_one()

        return {
            "total": int(row.total or 0),
            "allocated": int(allocated or 0),
            "avg_utilization_percent": round(float(row.util or 0.0), 1),
            "memory_used_mib": int(row.used or 0),
            "memory_total_mib": int(row.capacity or 0),
        }

    async def latest_metrics(self, gpu_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, GpuMetric]:
        """Most recent sample per GPU, in one query.

        A per-GPU query would be N round trips to render one dashboard page.
        DISTINCT ON is PostgreSQL-specific and rides the (gpu_id, recorded_at) index.
        """
        if not gpu_ids:
            return {}
        stmt = (
            select(GpuMetric)
            .where(GpuMetric.gpu_id.in_(gpu_ids))
            .order_by(GpuMetric.gpu_id, GpuMetric.recorded_at.desc())
            .distinct(GpuMetric.gpu_id)
        )
        return {m.gpu_id: m for m in (await self.session.execute(stmt)).scalars().all()}


class GpuMetricRepository(BaseRepository[GpuMetric]):
    model = GpuMetric

    async def history(
        self,
        gpu_id: uuid.UUID,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int = 500,
    ) -> Sequence[GpuMetric]:
        """Samples for one GPU, newest first.

        `limit` is capped by the caller. Returning a month of 15-second samples would
        be ~175k rows for a single chart.
        """
        stmt = select(GpuMetric).where(GpuMetric.gpu_id == gpu_id)
        if since is not None:
            stmt = stmt.where(GpuMetric.recorded_at >= since)
        if until is not None:
            stmt = stmt.where(GpuMetric.recorded_at <= until)
        stmt = stmt.order_by(GpuMetric.recorded_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def delete_older_than(self, cutoff: dt.datetime) -> int:
        """Enforce retention. Returns the number of rows removed."""
        result = cast(
            CursorResult[Any],
            await self.session.execute(delete(GpuMetric).where(GpuMetric.recorded_at < cutoff)),
        )
        return result.rowcount or 0

    async def count_all(self) -> int:
        return int(
            (await self.session.execute(select(func.count()).select_from(GpuMetric))).scalar_one()
        )


class GpuProcessRepository(BaseRepository[GpuProcess]):
    model = GpuProcess

    async def replace_for_gpu(self, gpu_id: uuid.UUID, processes: Sequence[GpuProcess]) -> None:
        """Swap in the current process set for one GPU.

        Current-state, not history: a process that exited must disappear, and nothing
        consumes historical occupancy. Delete-then-insert inside the caller's
        transaction, so a reader never sees an empty intermediate state.
        """
        await self.session.execute(delete(GpuProcess).where(GpuProcess.gpu_id == gpu_id))
        for process in processes:
            self.session.add(process)

    async def list_for_gpu(self, gpu_id: uuid.UUID) -> Sequence[GpuProcess]:
        stmt = select(GpuProcess).where(GpuProcess.gpu_id == gpu_id)
        return (await self.session.execute(stmt)).scalars().all()


class GpuHealthEventRepository(BaseRepository[GpuHealthEvent]):
    model = GpuHealthEvent

    async def recent(self, *, limit: int = 50) -> Sequence[GpuHealthEvent]:
        stmt = select(GpuHealthEvent).order_by(GpuHealthEvent.occurred_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def latest_for_gpu(self, gpu_id: uuid.UUID) -> GpuHealthEvent | None:
        stmt = (
            select(GpuHealthEvent)
            .where(GpuHealthEvent.gpu_id == gpu_id)
            .order_by(GpuHealthEvent.occurred_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class GpuAllocationRepository(BaseRepository[GpuAllocation]):
    model = GpuAllocation

    async def active_for_node(self, node_id: uuid.UUID) -> Sequence[GpuAllocation]:
        stmt = select(GpuAllocation).where(
            GpuAllocation.node_id == node_id, GpuAllocation.released_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def active_indices(self, node_id: uuid.UUID) -> set[int]:
        """GPU indices currently claimed on a node.

        Advisory only — used to *present* availability. Actual exclusion comes from the
        partial unique index, which is the only race-free place to enforce it.
        """
        return {a.gpu_index for a in await self.active_for_node(node_id)}

    async def by_reservation(self, reservation_id: uuid.UUID) -> Sequence[GpuAllocation]:
        stmt = select(GpuAllocation).where(GpuAllocation.reservation_id == reservation_id)
        return (await self.session.execute(stmt)).scalars().all()

    async def release_reservation(self, reservation_id: uuid.UUID) -> int:
        """Release every GPU in a reservation. Idempotent — cleanup paths retry."""
        allocations = [
            a for a in await self.by_reservation(reservation_id) if a.released_at is None
        ]
        now = dt.datetime.now(dt.UTC)
        for allocation in allocations:
            allocation.released_at = now
        return len(allocations)


class ContainerRepository(BaseRepository[Container]):
    model = Container

    async def get_by_container_id(self, node_id: uuid.UUID, container_id: str) -> Container | None:
        stmt = select(Container).where(
            Container.node_id == node_id, Container.container_id == container_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find_anywhere(self, container_id: str) -> Container | None:
        """Locate a container without knowing its node.

        Lets the API accept `/containers/{id}/stop` without the caller having to know
        or supply which host it lives on — the whole point of a control plane.
        """
        stmt = select(Container).where(Container.container_id == container_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_for_node(
        self, node_id: uuid.UUID, *, managed_only: bool = False
    ) -> Sequence[Container]:
        stmt = select(Container).where(Container.node_id == node_id)
        if managed_only:
            stmt = stmt.where(Container.managed.is_(True))
        stmt = stmt.order_by(Container.name)
        return (await self.session.execute(stmt)).scalars().all()

    async def list_all(self, *, managed_only: bool = False) -> Sequence[Container]:
        stmt = select(Container)
        if managed_only:
            stmt = stmt.where(Container.managed.is_(True))
        stmt = stmt.order_by(Container.node_id, Container.name)
        return (await self.session.execute(stmt)).scalars().all()

    async def delete_missing(self, node_id: uuid.UUID, seen_ids: Sequence[str]) -> int:
        """Drop cached rows for containers the node no longer reports.

        Without this, a removed container lingers in the UI forever. Guarded on an
        empty `seen_ids`: an agent that briefly returns nothing (Docker restarting)
        must not wipe the node's entire inventory.
        """
        if not seen_ids:
            return 0
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                delete(Container).where(
                    Container.node_id == node_id, Container.container_id.notin_(seen_ids)
                )
            ),
        )
        return result.rowcount or 0
