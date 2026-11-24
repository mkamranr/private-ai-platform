"""E80: a model's capabilities are read from the runtime once a copy of it is healthy."""

# ruff: noqa: F811  (pytest fixtures imported from the E73 tests)
from __future__ import annotations

import datetime as dt

import httpx
from sqlalchemy import select, update

from app.models.models_registry import Model, ModelDeployment
from app.workers import deployments as worker_module
from app.workers.deployments import DeploymentWorker
from tests.api.test_capability_probe import (  # noqa: F401  (fixture)
    fake_runtime,
    openai_like,
    runtime,
)


async def _state(database, name: str) -> Model:
    async with database.sessionmaker() as s:
        return (await s.execute(select(Model).where(Model.name == name))).scalar_one()


async def _healthy(database, name: str, *, at: dt.datetime, runtime_name: str = "vllm") -> None:
    async with database.sessionmaker() as s:
        model = (await s.execute(select(Model).where(Model.name == name))).scalar_one()
        model.runtime = runtime_name
        await s.execute(
            update(ModelDeployment)
            .where(ModelDeployment.model_id == model.id)
            .values(healthy_at=at)
        )
        await s.commit()


def _worker(app, settings, database) -> DeploymentWorker:
    return DeploymentWorker(
        settings, database.sessionmaker, app.state.secret_cipher, redis=app.state.redis.client
    )


class TestProbeOnDeploy:
    async def test_a_new_copy_is_asked_once(
        self, app, settings, database, committed_gateway, fake_runtime, monkeypatch
    ) -> None:
        monkeypatch.setattr(worker_module, "PROBES_PER_PASS", 1000)
        name = committed_gateway["model_name"]
        await _healthy(database, name, at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30))
        calls: list[str] = []
        transport = openai_like(tools=False)

        def counting(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return transport.handle_request(request)

        fake_runtime.append(runtime(counting))
        worker = _worker(app, settings, database)
        assert await worker.probe_new_copies() >= 1
        model = await _state(database, name)
        assert model.probed_capabilities == {"chat": True, "tools": False}
        asked = len(calls)
        assert asked >= 1

        # Not asked again until a copy next becomes healthy.
        await worker.probe_new_copies()
        assert len(calls) == asked
        await _healthy(
            database, name, at=model.capabilities_probed_at + dt.timedelta(microseconds=1)
        )
        await worker.probe_new_copies()
        assert len(calls) > asked

    async def test_a_failure_is_recorded_not_retried(
        self, app, settings, database, committed_gateway, fake_runtime, monkeypatch
    ) -> None:
        monkeypatch.setattr(worker_module, "PROBES_PER_PASS", 1000)
        name = committed_gateway["model_name"]
        await _healthy(database, name, at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30))
        calls: list[str] = []

        def down(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ConnectError("refused")

        fake_runtime.append(runtime(down))
        worker = _worker(app, settings, database)
        await worker.probe_new_copies()
        model = await _state(database, name)
        assert model.capabilities_probed_at is not None
        assert model.capabilities_probe_detail.startswith("The probe failed")
        asked = len(calls)
        await worker.probe_new_copies()
        assert len(calls) == asked

    async def test_off_or_without_valkey(
        self, app, settings, database, committed_gateway, fake_runtime
    ) -> None:
        name = committed_gateway["model_name"]
        await _healthy(database, name, at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=30))
        fake_runtime.append(openai_like())
        off = settings.model_copy(
            update={
                "models": settings.models.model_copy(update={"probe_capabilities_on_deploy": False})
            }
        )
        assert await _worker(app, off, database).probe_new_copies() == 0
        no_redis = DeploymentWorker(settings, database.sessionmaker, app.state.secret_cipher)
        assert await no_redis.probe_new_copies() == 0
        assert (await _state(database, name)).capabilities_probed_at is None
