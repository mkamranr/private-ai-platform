"""Alert notification channels and the delivery outbox (E13)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.models.notifications import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
)
from app.models.operations import Alert, AlertState
from app.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    channel_id: uuid.UUID
    alert_id: uuid.UUID
    dedupe_key: str
    status: str


class NotificationChannelRepository(BaseRepository[NotificationChannel]):
    model = NotificationChannel

    async def list_all(self, *, enabled_only: bool = False) -> Sequence[NotificationChannel]:
        stmt = select(NotificationChannel).order_by(NotificationChannel.name)
        if enabled_only:
            stmt = stmt.where(NotificationChannel.enabled.is_(True))
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_name(self, name: str) -> NotificationChannel | None:
        stmt = select(NotificationChannel).where(NotificationChannel.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()


class NotificationDeliveryRepository(BaseRepository[NotificationDelivery]):
    model = NotificationDelivery

    async def candidate_alerts(self, *, resolved_since: dt.datetime) -> Sequence[Alert]:
        """Open alerts, and alerts resolved since ``resolved_since``."""
        stmt = (
            select(Alert)
            .where(
                or_(
                    Alert.state != AlertState.RESOLVED,
                    Alert.resolved_at >= resolved_since,
                )
            )
            .order_by(Alert.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def keys_for(self, alert_ids: Sequence[uuid.UUID]) -> list[DeliveryKey]:
        if not alert_ids:
            return []
        stmt = select(
            NotificationDelivery.channel_id,
            NotificationDelivery.alert_id,
            NotificationDelivery.dedupe_key,
            NotificationDelivery.status,
        ).where(NotificationDelivery.alert_id.in_(alert_ids))
        rows = (await self.session.execute(stmt)).all()
        return [DeliveryKey(r[0], r[1], r[2], r[3]) for r in rows]

    async def enqueue(self, rows: list[dict[str, Any]]) -> int:
        """Insert deliveries, skipping any whose key already exists. Returns rows added."""
        if not rows:
            return 0
        stmt = (
            insert(NotificationDelivery)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_notification_deliveries_key")
            .returning(NotificationDelivery.id)
        )
        return len((await self.session.execute(stmt)).all())

    async def due(
        self, *, now: dt.datetime, limit: int
    ) -> list[tuple[NotificationDelivery, NotificationChannel]]:
        """Pending deliveries that are due, locked for this run.

        Fewest attempts first, then oldest: when a receiver fails, a run tries one message
        and defers the rest, so this lets each queued message take its turn instead of one
        failing message holding back the others until its retries run out.
        """
        stmt = (
            select(NotificationDelivery, NotificationChannel)
            .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
            .where(
                NotificationDelivery.status == DeliveryStatus.PENDING,
                NotificationDelivery.next_attempt_at <= now,
            )
            .order_by(NotificationDelivery.attempts, NotificationDelivery.created_at)
            .limit(limit)
            .with_for_update(of=NotificationDelivery, skip_locked=True)
        )
        return [(d, c) for d, c in (await self.session.execute(stmt)).all()]

    async def list_page(
        self,
        *,
        channel_id: uuid.UUID | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[NotificationDelivery], int]:
        conditions = []
        if channel_id is not None:
            conditions.append(NotificationDelivery.channel_id == channel_id)
        if status is not None:
            conditions.append(NotificationDelivery.status == status)
        stmt = (
            select(NotificationDelivery)
            .where(*conditions)
            .order_by(NotificationDelivery.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        total = await self.session.scalar(
            select(func.count()).select_from(NotificationDelivery).where(*conditions)
        )
        return (await self.session.execute(stmt)).scalars().all(), int(total or 0)

    async def pending_counts(self) -> dict[uuid.UUID, int]:
        stmt = (
            select(NotificationDelivery.channel_id, func.count())
            .where(NotificationDelivery.status == DeliveryStatus.PENDING)
            .group_by(NotificationDelivery.channel_id)
        )
        return {r[0]: int(r[1]) for r in (await self.session.execute(stmt)).all()}

    async def cancel_pending(self, channel_id: uuid.UUID, *, reason: str) -> int:
        stmt = (
            update(NotificationDelivery)
            .where(
                NotificationDelivery.channel_id == channel_id,
                NotificationDelivery.status == DeliveryStatus.PENDING,
            )
            .values(status=DeliveryStatus.CANCELLED, last_error=reason)
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def prune(self, *, before: dt.datetime) -> int:
        """Delete finished deliveries older than ``before``."""
        stmt = delete(NotificationDelivery).where(
            NotificationDelivery.status != DeliveryStatus.PENDING,
            NotificationDelivery.created_at < before,
        )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
