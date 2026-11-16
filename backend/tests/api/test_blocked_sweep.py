"""The deployment worker stops what blocking could not (E28).

E26 stops a model's deployments when it is blocked, but one whose node is unreachable is
left running and reported once. The worker now retries every minute, alerts while it
cannot stop one, and also stops a deployment that was still being created.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import SecretCipher
from app.models.audit import AuditAction, AuditLog
from app.models.models_registry import DeploymentState
from app.models.operations import Alert, AlertState
from app.services.deployment import WORKER_ACTOR, DeploymentService
from app.workers.deployments import BLOCKED_RUNNING_PREFIX, DeploymentWorker
from tests.api.test_block_stops_deployments import Unreachable
from tests.api.test_model_governance import _model, _serving


def _worker(settings, database) -> DeploymentWorker:
    return DeploymentWorker(settings, database.sessionmaker, SecretCipher(settings.security))


async def _alert(session: AsyncSession, deployment_id: Any) -> Alert | None:
    fingerprint = f"{BLOCKED_RUNNING_PREFIX}{deployment_id}"
    rows = await session.execute(select(Alert).where(Alert.fingerprint == fingerprint))
    return rows.scalars().first()


class TestSweep:
    async def test_a_blocked_models_running_deployment_is_stopped(
        self, session: AsyncSession, settings, database
    ) -> None:
        model = await _model(session, approval_status="blocked")
        deployment = await _serving(session, model)

        stopped, failed = await _worker(settings, database)._stop_blocked(session)
        assert deployment.id in stopped and failed == []
        assert str(deployment.state) == DeploymentState.STOPPED
        (audit,) = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.MODEL_STOPPED,
                        AuditLog.resource_id == str(deployment.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audit.username == WORKER_ACTOR and audit.user_id is None
        assert audit.message == f"Model {model.name} is blocked"

    async def test_one_still_being_created_is_stopped(
        self, session: AsyncSession, settings, database
    ) -> None:
        model = await _model(session, approval_status="blocked")
        deployment = await _serving(session, model)
        deployment.state = DeploymentState.SCHEDULING
        await session.flush()
        stopped, _ = await _worker(settings, database)._stop_blocked(session)
        assert deployment.id in stopped
        assert str(deployment.state) == DeploymentState.STOPPED

    async def test_other_models_are_left_alone(
        self, session: AsyncSession, settings, database
    ) -> None:
        for status in ("approved", "deprecated", "experimental"):
            deployment = await _serving(session, await _model(session, approval_status=status))
            stopped, _ = await _worker(settings, database)._stop_blocked(session)
            assert deployment.id not in stopped
            assert str(deployment.state) == DeploymentState.RUNNING

    async def test_one_that_cannot_be_stopped_alerts_until_it_is(
        self, session: AsyncSession, settings, database, monkeypatch
    ) -> None:
        model = await _model(session, approval_status="blocked")
        stuck = await _serving(session, model)
        stuck.container_id = "ctr-stuck"
        await session.flush()
        worker = _worker(settings, database)

        original = DeploymentService._backend
        monkeypatch.setattr(
            DeploymentService,
            "_backend",
            lambda self, node: Unreachable() if node.id == stuck.node_id else original(self, node),
        )
        stopped, failed = await worker._stop_blocked(session)
        assert stuck.id in failed and stuck.id not in stopped
        assert str(stuck.state) == DeploymentState.RUNNING
        alert = await _alert(session, stuck.id)
        assert alert is not None and alert.state == AlertState.ACTIVE
        assert alert.severity == "WARNING" and alert.source == "deployment"

        # The node is back: the next sweep stops it and clears the alert.
        monkeypatch.setattr(DeploymentService, "_backend", lambda self, node: _Reachable())
        stopped, failed = await worker._stop_blocked(session)
        assert stuck.id in stopped and failed == []
        assert str(stuck.state) == DeploymentState.STOPPED
        await session.refresh(alert)
        assert alert.state != AlertState.ACTIVE


class _Reachable:
    async def stop_model(self, handle: Any) -> Any:
        return handle
