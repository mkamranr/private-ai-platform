"""Zero-downtime deployment revisions (E61).

The deployment service runs for real on the test database; only the compute backend is a
fake, so the state machine, the handover and the rollout are the real ones. ``_drive``
does what one deployment-worker pass does.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.interfaces.compute import DeploymentHandle
from app.core.interfaces.compute import DeploymentState as BackendState
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.models.models_registry import (
    DeploymentRevision,
    DeploymentState,
    ModelDeployment,
    RolloutState,
)
from app.repositories.audit import AuditRepository
from app.repositories.infrastructure import (
    GpuAllocationRepository,
    GpuHealthEventRepository,
    GpuMetricRepository,
    GpuProcessRepository,
    GpuRepository,
    NodeRepository,
)
from app.repositories.models_registry import ModelDeploymentRepository, ModelRepository
from app.services.audit import AuditService
from app.services.deployment import DeploymentService, RevisionChange
from app.services.infrastructure import GpuService
from tests.api.test_model_governance import _model, _serving
from tests.conftest import auth_header


class FakeBackend:
    """Starts containers that become healthy (or not), and records what it stops."""

    def __init__(self) -> None:
        self.healthy = True
        self.stop_fails = False
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def deploy_model(self, request: Any) -> DeploymentHandle:
        ref = f"c-{uuid.uuid4().hex[:10]}"
        self.started.append(ref)
        return DeploymentHandle(
            deployment_id=request.deployment_id,
            state=BackendState.CREATING,
            backend_ref=ref,
            internal_url=f"http://{ref}:8000",
        )

    async def wait_until_healthy(self, handle: DeploymentHandle, **_: Any) -> DeploymentHandle:
        return DeploymentHandle(
            deployment_id=handle.deployment_id,
            state=BackendState.RUNNING if self.healthy else BackendState.FAILED,
            backend_ref=handle.backend_ref,
            internal_url=handle.internal_url,
            message=None if self.healthy else "CUDA out of memory",
        )

    async def stop_model(self, handle: DeploymentHandle) -> DeploymentHandle:
        if self.stop_fails:
            raise ConnectionError("node agent unreachable")
        self.stopped.append(handle.backend_ref)
        return handle

    async def fetch_logs(self, handle: DeploymentHandle, *, tail: int = 200) -> str:
        return "torch.OutOfMemoryError"


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def service(session: AsyncSession, settings, backend: FakeBackend) -> DeploymentService:
    gpus = GpuService(
        settings,
        GpuRepository(session),
        GpuMetricRepository(session),
        GpuProcessRepository(session),
        GpuHealthEventRepository(session),
        GpuAllocationRepository(session),
    )
    return DeploymentService(
        settings,
        ModelRepository(session),
        ModelDeploymentRepository(session),
        NodeRepository(session),
        gpus,
        AuditService(AuditRepository(session)),
        lambda node: backend,  # type: ignore[arg-type,return-value]
    )


async def _drive(service: DeploymentService, passes: int = 1) -> None:
    """What the deployment worker does each pass."""
    for _ in range(passes):
        for deployment in await service._deployments.list_pending():
            await service.advance(deployment)
        await service.complete_handovers()
        await service.rollouts().step()


async def _copy(session: AsyncSession, model: Any) -> ModelDeployment:
    deployment = await _serving(session, model)
    deployment.container_id = f"old-{uuid.uuid4().hex[:8]}"
    await session.flush()
    return deployment


async def _revisions(session: AsyncSession, deployment: ModelDeployment) -> list:
    rows = await session.execute(
        select(DeploymentRevision)
        .where(DeploymentRevision.deployment_id == deployment.id)
        .order_by(DeploymentRevision.revision)
    )
    return list(rows.scalars())


def _change(**fields: Any) -> RevisionChange:
    return RevisionChange(**{"max_model_len": 2048, **fields})


class TestChoosingTheStrategy:
    async def test_the_only_copy_hands_over_to_a_new_one(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)
        assert outcome.strategy == "blue_green" and outcome.downtime is False
        # Nothing about the original changed yet: it keeps serving revision 1.
        assert original.state == DeploymentState.RUNNING
        assert original.current_revision == 1 and original.max_model_len == 4096
        successor = await service.find_deployment(outcome.successor_id)
        assert successor is not None and successor.replaces_id == original.id
        assert successor.max_model_len == 2048 and successor.state == DeploymentState.SCHEDULING
        assert [r.revision for r in await _revisions(session, original)] == [1, 2]

    async def test_with_another_copy_running_it_is_in_place(self, service, session, model_admin):
        model = await _model(session)
        first = await _copy(session, model)
        await _copy(session, model)
        outcome = await service.revise_deployment(first.id, _change(), actor=model_admin)
        assert outcome.strategy == "in_place" and outcome.downtime is False
        assert "other 1 running copy" in outcome.message
        assert first.state == DeploymentState.SCHEDULING and first.current_revision == 2
        assert not await service.has_successor(first.id)

    async def test_not_serving_is_in_place(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        original.state = DeploymentState.FAILED
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)
        assert outcome.strategy == "in_place" and outcome.downtime is False

    async def test_in_place_on_request_says_it_means_downtime(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        outcome = await service.revise_deployment(
            original.id, _change(), actor=model_admin, strategy="in_place"
        )
        assert outcome.strategy == "in_place" and outcome.downtime is True
        assert "unavailable" in outcome.message

    async def test_no_free_gpus(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        original.gpu_indices = [0]  # nothing registered, so no second copy fits
        await session.flush()
        chosen, downtime, message, placement = await service._choose_strategy(
            original, model, "auto"
        )
        assert (chosen, downtime, placement) == ("in_place", True, None)
        assert "No free GPUs" in message
        with pytest.raises(ConflictError, match="No free GPUs"):
            await service._choose_strategy(original, model, "blue_green")


class TestHandover:
    async def test_the_new_copy_takes_over_the_original_row(
        self, service, session, backend, model_admin
    ):
        model = await _model(session)
        original = await _copy(session, model)
        old_container = original.container_id
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)

        await _drive(service, passes=2)
        await session.refresh(original)
        assert original.state == DeploymentState.RUNNING
        assert original.current_revision == 2 and original.max_model_len == 2048
        assert original.container_id == backend.started[-1]
        assert "without downtime" in (original.state_detail or "")
        # The container the original ran is stopped, and the copy's row is gone.
        assert backend.stopped == [old_container]
        assert await service.find_deployment(outcome.successor_id) is None
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.DEPLOYMENT_HANDOVER,
                    AuditLog.resource_id == str(original.id),
                )
            )
        ).scalar_one()
        assert audit.meta["outcome"] == "took over"

    async def test_a_failed_copy_leaves_the_original_serving(
        self, service, session, backend, model_admin
    ):
        model = await _model(session)
        original = await _copy(session, model)
        old_container = original.container_id
        backend.healthy = False
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)

        await _drive(service, passes=2)
        await session.refresh(original)
        assert original.state == DeploymentState.RUNNING
        assert original.current_revision == 1 and original.container_id == old_container
        assert "failed to start on a new copy" in (original.state_detail or "")
        assert await service.find_deployment(outcome.successor_id) is None
        revision = (await _revisions(session, original))[-1]
        assert "Not applied" in (revision.change_note or "")
        assert old_container not in backend.stopped

    async def test_one_revision_at_a_time(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        await service.revise_deployment(original.id, _change(), actor=model_admin)
        with pytest.raises(ConflictError, match="new copy"):
            await service.revise_deployment(
                original.id, _change(max_model_len=1024), actor=model_admin
            )

    async def test_stopping_the_original_stops_the_new_copy(self, service, session, model_admin):
        model = await _model(session)
        original = await _copy(session, model)
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)
        await service.stop_deployment(original.id, actor=model_admin)
        assert await service.find_deployment(outcome.successor_id) is None
        await _drive(service, passes=2)
        await session.refresh(original)
        assert original.state == DeploymentState.STOPPED

    async def test_an_old_container_that_cannot_be_stopped_stays_visible(
        self, service, session, backend, model_admin
    ):
        model = await _model(session)
        original = await _copy(session, model)
        old_container = original.container_id
        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)
        await _drive(service)  # the copy is created...
        backend.stop_fails = True
        await _drive(service)  # ...becomes healthy and takes over
        await session.refresh(original)
        assert original.current_revision == 2
        leftover = await service.find_deployment(outcome.successor_id)
        assert leftover is not None and leftover.state == DeploymentState.FAILED
        assert leftover.container_id == old_container and leftover.replaces_id is None
        assert "could not be stopped" in (leftover.error_message or "")


class TestRollout:
    async def test_every_copy_one_at_a_time(self, service, session, backend, model_admin):
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        rollout = await service.rollouts().start(model.id, _change(), actor=model_admin)

        await _drive(service)
        revised = [d for d in (first, second) if d.current_revision == 2]
        assert len(revised) == 1  # one copy at a time
        waiting = second if revised[0] is first else first
        assert waiting.state == DeploymentState.RUNNING and waiting.current_revision == 1

        await _drive(service, passes=4)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.DONE and rollout.done_count == 2
        for copy in (first, second):
            await session.refresh(copy)
            assert copy.state == DeploymentState.RUNNING
            assert copy.current_revision == 2 and copy.max_model_len == 2048

    async def test_a_single_copy_rolls_out_without_downtime(self, service, session, model_admin):
        model = await _model(session)
        only = await _copy(session, model)
        rollout = await service.rollouts().start(model.id, _change(), actor=model_admin)
        await _drive(service, passes=4)
        await session.refresh(rollout)
        await session.refresh(only)
        assert rollout.state == RolloutState.DONE
        assert only.current_revision == 2 and only.state == DeploymentState.RUNNING

    async def test_a_failure_stops_before_the_next_copy(
        self, service, session, backend, model_admin
    ):
        model = await _model(session)
        first = await _copy(session, model)
        second = await _copy(session, model)
        backend.healthy = False
        rollout = await service.rollouts().start(model.id, _change(), actor=model_admin)
        await _drive(service, passes=4)
        await session.refresh(rollout)
        assert rollout.state == RolloutState.FAILED
        assert "did not come back" in (rollout.error or "")
        untouched = [d for d in (first, second) if d.current_revision == 1]
        assert len(untouched) == 1 and untouched[0].state == DeploymentState.RUNNING


class TestApi:
    async def test_the_response_says_how(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        original = await _serving(session, model)
        response = await client.post(
            f"/api/v1/deployments/{original.id}/revisions",
            headers=auth_header(tokens, model_admin),
            json={"max_model_len": 2048},
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["strategy"] == "blue_green" and body["downtime"] is False
        assert body["current"] is False and body["successor_id"]
        listing = await client.get("/api/v1/deployments", headers=auth_header(tokens, model_admin))
        copies = [d for d in listing.json() if d.get("replaces_id") == str(original.id)]
        assert len(copies) == 1

    async def test_blue_green_needs_a_running_deployment(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        original = await _serving(session, model)
        original.state = DeploymentState.STOPPED
        await session.flush()
        response = await client.post(
            f"/api/v1/deployments/{original.id}/revisions",
            headers=auth_header(tokens, model_admin),
            json={"max_model_len": 2048, "strategy": "blue_green"},
        )
        assert response.status_code == 409

    async def test_rollouts(
        self, client: AsyncClient, tokens, model_admin: User, session: AsyncSession
    ) -> None:
        model = await _model(session)
        await _serving(session, model)
        await _serving(session, model)
        headers = auth_header(tokens, model_admin)
        url = f"/api/v1/models/{model.id}/rollout"
        assert (await client.get(url, headers=headers)).json() is None

        refused = await client.post(url, headers=headers, json={"image": "evil/image:latest"})
        assert refused.status_code == 422 and "every copy" in refused.text

        started = await client.post(
            url, headers=headers, json={"max_model_len": 2048, "change_note": "smaller"}
        )
        assert started.status_code == 202, started.text
        assert started.json()["state"] == "RUNNING"
        assert len(started.json()["deployment_ids"]) == 2
        again = await client.post(url, headers=headers, json={"max_model_len": 1024})
        assert again.status_code == 409

        cancelled = await client.delete(url, headers=headers)
        assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
        assert (await client.get(url, headers=headers)).json()["state"] == "CANCELLED"
        assert (await client.delete(url, headers=headers)).status_code == 404


class TestOnGpus:
    async def test_the_new_copy_takes_free_gpus_and_frees_the_old_ones(
        self, service, session, model_admin
    ):
        from app.core.interfaces.scheduler import Placement, PlacementRequest
        from app.models.infrastructure import Gpu, Node, NodeStatus

        node = Node(
            name=f"gpu-node-{uuid.uuid4().hex[:6]}",
            agent_url="http://agent:9100",
            agent_token_encrypted="x",
            status=NodeStatus.ONLINE,
        )
        session.add(node)
        await session.flush()
        for index in (0, 1):
            session.add(
                Gpu(
                    node_id=node.id,
                    index=index,
                    uuid=f"GPU-{uuid.uuid4()}",
                    name="NVIDIA A100-SXM4-80GB",
                    memory_total_mib=81920,
                )
            )
        await session.flush()
        model = await _model(session)
        original = await _copy(session, model)
        held = await service._scheduler.reserve(
            PlacementRequest(
                model_id=str(model.id),
                gpu_count=1,
                required_memory_mib_per_gpu=0,
                node_id=str(node.id),
                gpu_indices=(0,),
            ),
            Placement(node_id=str(node.id), gpu_indices=(0,), reservation_id="", rationale="t"),
        )
        original.node_id = node.id
        original.gpu_indices = [0]
        original.reservation_id = uuid.UUID(held.reservation_id)
        await session.flush()

        outcome = await service.revise_deployment(original.id, _change(), actor=model_admin)
        assert outcome.strategy == "blue_green"
        successor = await service.find_deployment(outcome.successor_id)
        assert successor is not None and successor.gpu_indices == [1]
        assert successor.reservation_id is not None

        await _drive(service, passes=2)
        await session.refresh(original)
        assert original.gpu_indices == [1] and original.current_revision == 2
        free = set(await service._gpus.free_indices(node.id))
        assert 0 in free and 1 not in free
