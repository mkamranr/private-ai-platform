"""E77: weighted copies, and rollouts that watch a canary before the rest."""

# ruff: noqa: F811  (pytest fixtures imported from the E61 tests)
from __future__ import annotations

import datetime as dt
import uuid
from collections import Counter

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.models_registry import ModelDeployment, RolloutState, UsageRecord
from app.services.replica_balancer import ReplicaBalancer
from tests.api.test_model_governance import _model, _serving
from tests.api.test_replica_balancing import MemoryRedis
from tests.api.test_zero_downtime_revisions import (  # noqa: F401  (fixtures)
    _change,
    _copy,
    _drive,
    backend,
    service,
)
from tests.conftest import auth_header


class _Dep:
    def __init__(self, weight: int) -> None:
        self.id = uuid.uuid4()
        self.traffic_weight = weight


class TestWeights:
    async def test_share_follows_the_weights(self) -> None:
        for strategy in ("least_busy", "round_robin"):
            balancer = ReplicaBalancer(MemoryRedis(), 600, strategy)
            canary, a, b = _Dep(10), _Dep(100), _Dep(100)
            model = uuid.uuid4()
            firsts = Counter(
                [(await balancer.order(model, [canary, a, b]))[0].id for _ in range(420)]
            )
            # 10 of 210: about 20 of 420 calls.
            assert 10 <= firsts[canary.id] <= 30, (strategy, firsts[canary.id])
            assert abs(firsts[a.id] - firsts[b.id]) < 40

    async def test_weight_zero_is_failover_only(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        drained, live = _Dep(0), _Dep(100)
        for _ in range(20):
            ordered = await balancer.order(uuid.uuid4(), [drained, live])
            assert ordered == [live, drained]

    async def test_equal_weights_keep_the_old_spread(self) -> None:
        balancer = ReplicaBalancer(MemoryRedis(), 600, "least_busy")
        copies = [_Dep(50), _Dep(50), _Dep(50)]
        model = uuid.uuid4()
        firsts = Counter([(await balancer.order(model, copies))[0].id for _ in range(9)])
        assert sorted(firsts.values()) == [3, 3, 3]

    async def test_set_through_the_api(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        copy = await _serving(session, model)
        headers = auth_header(tokens, model_admin)
        url = f"/api/v1/deployments/{copy.id}/traffic-weight"
        response = await client.put(url, json={"traffic_weight": 101}, headers=headers)
        assert response.status_code == 422
        response = await client.put(url, json={"traffic_weight": 25}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["traffic_weight"] == 25
        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == str(copy.id))
                )
            )
            .scalars()
            .all()
        )
        assert "DEPLOYMENT_TRAFFIC_WEIGHT_CHANGED" in actions


async def _calls(
    session: AsyncSession, copy_id: str, ok: int, failed: int, latency_ms: float = 100.0
) -> None:
    for status in [200] * ok + [502] * failed:
        session.add(
            UsageRecord(
                endpoint="chat",
                requested_model="m",
                model="m",
                deployment_id=uuid.UUID(str(copy_id)),
                status_code=status,
                latency_ms=latency_ms,
            )
        )
    await session.flush()


async def _watch_ends(session: AsyncSession, rollout) -> None:
    """Move the watch into the past, as if its minutes had gone by."""
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    rollout.canary = {
        **rollout.canary,
        "started_at": past.isoformat(),
        "until": (past + dt.timedelta(minutes=1)).isoformat(),
    }
    await session.flush()


CANARY = {"percent": 10, "minutes": 15, "max_error_percent": 5.0}


class TestCanary:
    async def test_needs_two_copies(self, service, session, model_admin) -> None:
        model = await _model(session)
        await _copy(session, model)
        with pytest.raises(ValidationError, match="at least two"):
            await service.rollouts().start(model.id, _change(), actor=model_admin, canary=CANARY)

    async def test_passes_then_the_rest_follow(self, service, session, model_admin) -> None:
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        rollout = await service.rollouts().start(
            model.id, _change(), actor=model_admin, canary=CANARY
        )
        await _drive(service, passes=4)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.RUNNING and rollout.done_count == 1
        assert rollout.canary["state"] == "watching"
        watched = await session.get(ModelDeployment, uuid.UUID(rollout.canary["deployment_id"]))
        other = second if watched.id == first.id else first
        assert watched.traffic_weight == 10 and watched.current_revision == 2
        assert other.current_revision == 1  # held while the canary is watched

        await _calls(session, rollout.canary["deployment_id"], ok=99, failed=1)
        await _watch_ends(session, rollout)
        await _drive(service, passes=4)
        await session.refresh(rollout)
        await session.refresh(watched)
        await session.refresh(other)
        assert rollout.state == RolloutState.DONE
        assert rollout.canary["state"] == "passed" and rollout.canary["calls"] == 100
        assert watched.traffic_weight == 100 and other.current_revision == 2

    async def test_too_many_errors_stops_it(self, service, session, model_admin) -> None:
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        rollout = await service.rollouts().start(
            model.id, _change(), actor=model_admin, canary=CANARY
        )
        await _drive(service, passes=4)
        await session.refresh(rollout)
        watched_id = rollout.canary["deployment_id"]
        await _calls(session, watched_id, ok=80, failed=20)
        await _watch_ends(session, rollout)
        await _drive(service, passes=2)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.FAILED
        assert rollout.canary["state"] == "failed" and "20 of 100" in rollout.error
        watched = await session.get(ModelDeployment, uuid.UUID(watched_id))
        other = second if watched.id == first.id else first
        await session.refresh(watched)
        assert watched.traffic_weight == 0 and other.current_revision == 1

    async def test_cancel_gives_it_back_its_share(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession, service
    ) -> None:
        model = await _model(session)
        await _copy(session, model)
        await _copy(session, model)
        headers = auth_header(tokens, model_admin)
        url = f"/api/v1/models/{model.id}/rollout"
        started = await client.post(
            url,
            headers=headers,
            json={"max_model_len": 2048, "canary_percent": 10, "canary_minutes": 30},
        )
        assert started.status_code == 202, started.text
        assert started.json()["canary"] == {
            "percent": 10,
            "minutes": 30,
            "max_error_percent": 1.0,
            "copies": 1,
            "max_latency_ratio": None,
        }
        await _drive(service, passes=4)
        body = (await client.get(url, headers=headers)).json()
        assert body["canary"]["state"] == "watching"
        watched = uuid.UUID(body["canary"]["deployment_id"])
        cancelled = await client.delete(url, headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["canary"]["state"] == "cancelled"
        copy = await session.get(ModelDeployment, watched)
        await session.refresh(copy)
        assert copy.traffic_weight == 100


class TestSmarterCanary:
    """E82: more than one canary copy, and latency against the old copies."""

    async def test_copies_must_leave_one_old(self, service, session, model_admin) -> None:
        model = await _model(session)
        await _copy(session, model)
        await _copy(session, model)
        with pytest.raises(ValidationError, match="at most 1 can be canaries"):
            await service.rollouts().start(
                model.id, _change(), actor=model_admin, canary={**CANARY, "copies": 2}
            )

    async def test_two_canaries_watched_together(self, service, session, model_admin) -> None:
        model = await _model(session)
        copies = [await _copy(session, model) for _ in range(3)]
        rollout = await service.rollouts().start(
            model.id, _change(), actor=model_admin, canary={**CANARY, "copies": 2}
        )
        await _drive(service, passes=8)
        await session.refresh(rollout)
        assert rollout.canary["state"] == "watching" and rollout.done_count == 2
        watched = rollout.canary["deployment_ids"]
        assert len(watched) == 2
        for copy in copies:
            await session.refresh(copy)
        by_id = {str(c.id): c for c in copies}
        assert all(
            by_id[i].traffic_weight == 10 and by_id[i].current_revision == 2 for i in watched
        )
        (old,) = [c for c in copies if str(c.id) not in watched]
        assert old.current_revision == 1 and old.traffic_weight == 100

        # Errors are counted across both canaries: 3 of 100 is over a 2% limit.
        await _calls(session, watched[0], ok=50, failed=3)
        await _calls(session, watched[1], ok=47, failed=0)
        rollout.canary = {**rollout.canary, "max_error_percent": 2.0}
        await _watch_ends(session, rollout)
        await _drive(service, passes=2)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.FAILED and "3 of 100" in rollout.error
        for ident in watched:
            await session.refresh(by_id[ident])
            assert by_id[ident].traffic_weight == 0

    async def test_slow_canary_stops_it(self, service, session, model_admin) -> None:
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        rollout = await service.rollouts().start(
            model.id,
            _change(),
            actor=model_admin,
            canary={**CANARY, "max_latency_ratio": 1.5},
        )
        await _drive(service, passes=4)
        await session.refresh(rollout)
        watched = rollout.canary["deployment_id"]
        other = second if str(first.id) == watched else first
        await _calls(session, watched, ok=20, failed=0, latency_ms=400)
        await _calls(session, str(other.id), ok=40, failed=0, latency_ms=200)
        await _watch_ends(session, rollout)
        await _drive(service, passes=2)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.FAILED
        assert "p95 latency 400 ms against 200 ms" in rollout.error
        assert rollout.canary["latency_p95_ms"] == 400

    async def test_latency_not_judged_on_few_calls(self, service, session, model_admin) -> None:
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        rollout = await service.rollouts().start(
            model.id,
            _change(),
            actor=model_admin,
            canary={**CANARY, "max_latency_ratio": 1.5},
        )
        await _drive(service, passes=4)
        await session.refresh(rollout)
        watched = rollout.canary["deployment_id"]
        other = second if str(first.id) == watched else first
        await _calls(session, watched, ok=3, failed=0, latency_ms=900)
        await _calls(session, str(other.id), ok=40, failed=0, latency_ms=100)
        await _watch_ends(session, rollout)
        await _drive(service, passes=4)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.DONE
        assert rollout.canary["state"] == "passed"
        assert "Latency not judged" in rollout.canary["latency_note"]

    async def test_cancel_before_the_watch_restores_shares(
        self, service, session, model_admin
    ) -> None:
        model = await _model(session)
        copies = [await _copy(session, model) for _ in range(3)]
        rollout = await service.rollouts().start(
            model.id, _change(), actor=model_admin, canary={**CANARY, "copies": 2}
        )
        # Drive until the first canary has its share but the second isn't done.
        for _ in range(8):
            await _drive(service)
            await session.refresh(rollout)
            if (rollout.canary or {}).get("deployment_ids"):
                break
        assert rollout.canary.get("state") is None
        given = rollout.canary["deployment_ids"]
        await service.rollouts().cancel(model.id, actor=model_admin)
        for copy in copies:
            await session.refresh(copy)
            if str(copy.id) in given:
                assert copy.traffic_weight == 100
