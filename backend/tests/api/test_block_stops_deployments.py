"""Blocking a model stops its running deployments (E26).

Before E26 a blocked model was refused by the gateway but its containers kept running,
holding GPUs for a model nothing could call.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.models.models_registry import DeploymentState, ModelDeployment
from app.services.deployment import DeploymentService
from tests.api.test_model_governance import _model, _serving, approver  # noqa: F401
from tests.conftest import auth_header


async def _block(client: AsyncClient, tokens, user: User, model_id: Any, **extra: Any):
    return await client.post(
        f"/api/v1/models/{model_id}/approval",
        headers=auth_header(tokens, user),
        json={"status": "blocked", "note": "CVE-2026-0002", **extra},
    )


async def _state(session: AsyncSession, deployment: ModelDeployment) -> str:
    await session.refresh(deployment)
    return str(deployment.state)


class Unreachable:
    async def stop_model(self, handle: Any) -> Any:
        raise ConnectionError("node agent unreachable")


class TestBlockStops:
    async def test_blocking_stops_running_deployments(
        self, client: AsyncClient, tokens, approver: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        first = await _serving(session, model)
        second = await _serving(session, model)

        response = await _block(client, tokens, approver, model.id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["approval_status"] == "blocked"
        assert set(body["stopped_deployments"]) == {str(first.id), str(second.id)}
        assert body["stop_failed"] == []
        for deployment in (first, second):
            assert await _state(session, deployment) == DeploymentState.STOPPED
            assert deployment.internal_url is None

        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == AuditAction.MODEL_STOPPED,
                        AuditLog.resource_id == str(first.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [a.message for a in audits] == [f"Model {model.name} blocked"]

    async def test_stop_deployments_false_leaves_them_running(
        self, client: AsyncClient, tokens, approver: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        response = await _block(client, tokens, approver, model.id, stop_deployments=False)
        assert response.status_code == 200
        assert response.json()["stopped_deployments"] == []
        assert await _state(session, deployment) == DeploymentState.RUNNING

    async def test_other_statuses_stop_nothing(
        self, client: AsyncClient, tokens, approver: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        deployment = await _serving(session, model)
        response = await client.post(
            f"/api/v1/models/{model.id}/approval",
            headers=auth_header(tokens, approver),
            json={"status": "deprecated", "stop_deployments": True},
        )
        assert response.status_code == 200
        assert response.json()["stopped_deployments"] == []
        assert await _state(session, deployment) == DeploymentState.RUNNING

    async def test_an_unreachable_node_is_reported_not_fatal(
        self, client: AsyncClient, tokens, approver: User, session: AsyncSession, monkeypatch
    ) -> None:
        """The block still applies; the deployment that could not be stopped is listed."""
        model = await _model(session)
        reachable = await _serving(session, model)
        stuck = await _serving(session, model)
        stuck.container_id = "ctr-stuck"
        await session.flush()

        original = DeploymentService._backend

        def backend(self, node):
            return Unreachable() if node.id == stuck.node_id else original(self, node)

        monkeypatch.setattr(DeploymentService, "_backend", backend)
        response = await _block(client, tokens, approver, model.id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["approval_status"] == "blocked"
        assert body["stopped_deployments"] == [str(reachable.id)]
        assert body["stop_failed"] == [str(stuck.id)]
        assert await _state(session, stuck) == DeploymentState.RUNNING
