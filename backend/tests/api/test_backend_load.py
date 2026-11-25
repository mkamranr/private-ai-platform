"""E81: inference backends' load kept over time, and alerted on."""

# ruff: noqa: F811  (pytest fixtures imported from the E1 tests)
from __future__ import annotations

import datetime as dt
import uuid

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import BackendsSettings
from app.core.security import SecretCipher
from app.models.operations import Alert, AlertState, BackendMetricSample, InferenceBackend
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository, InferenceBackendRepository
from app.services.alerts import AlertService, backend_load_fingerprint
from app.services.audit import AuditService
from app.services.inference_backends import (
    InferenceBackendService,
    backend_levels,
    load_breach,
)
from tests.api.test_backends import (  # noqa: F401  (fixtures)
    _create,
    backend_admin,
    backend_viewer,
    fake_backend,
)
from tests.conftest import auth_header

LEVELS = BackendsSettings(load_alert_requests_waiting=5, load_alert_kv_cache_percent=90)


def vllm(waiting: float, cache: float):
    text = (
        f'vllm:num_requests_running{{model_name="q"}} 2.0\n'
        f'vllm:num_requests_waiting{{model_name="q"}} {waiting}\n'
        f'vllm:gpu_cache_usage_perc{{model_name="q"}} {cache / 100}\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(200, text=text)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "q"}]})
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "0.12"})
        return httpx.Response(404)

    return handler


def _service(session: AsyncSession, settings) -> InferenceBackendService:
    audit = AuditService(AuditRepository(session))
    return InferenceBackendService(
        InferenceBackendRepository(session),
        SecretCipher(settings.security),
        audit,
        AlertService(AlertRepository(session), audit),
    )


async def _alert(session: AsyncSession, backend_id) -> Alert | None:
    return (
        await session.execute(
            select(Alert).where(
                Alert.fingerprint == backend_load_fingerprint(backend_id),
                Alert.state != AlertState.RESOLVED,
            )
        )
    ).scalar_one_or_none()


class TestBreach:
    def test_levels(self) -> None:
        assert load_breach({"requests_waiting": 7, "kv_cache_usage_percent": 95}, LEVELS) == [
            "7 requests waiting",
            "KV cache 95% used",
        ]
        assert load_breach({"requests_waiting": 4, "kv_cache_usage_percent": 50}, LEVELS) == []
        assert load_breach({"requests_waiting": 100}, BackendsSettings()) == []


class TestRecorded:
    async def test_samples_and_an_alert_after_two_checks(
        self, client: AsyncClient, tokens, backend_admin, fake_backend, session, settings
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(
            client, headers, backend_type="vllm", base_url="http://vllm-host:8000"
        )
        backend_id = uuid.UUID(str(created["id"]))
        service = _service(session, settings)

        fake_backend["vllm-host"] = vllm(waiting=8, cache=95)
        await service.check_all_enabled(load=LEVELS)
        assert await _alert(session, backend_id) is None  # one check over is not enough
        await service.check_all_enabled(load=LEVELS)
        alert = await _alert(session, backend_id)
        assert alert is not None and "8 requests waiting" in alert.title
        assert "KV cache 95% used" in alert.title

        fake_backend["vllm-host"] = vllm(waiting=0, cache=40)
        await service.check_all_enabled(load=LEVELS)
        await session.refresh(alert)
        assert alert.state == AlertState.RESOLVED

        response = await client.get(
            f"/api/v1/backends/{backend_id}/metrics/history?since_minutes=30", headers=headers
        )
        assert response.status_code == 200, response.text
        samples = response.json()["samples"]
        assert [s["values"]["requests_waiting"] for s in samples] == [8.0, 8.0, 0.0]
        assert samples[-1]["values"]["kv_cache_usage_percent"] == 40.0

    async def test_nothing_without_metrics_or_when_down(
        self, client: AsyncClient, tokens, backend_admin, fake_backend, session, settings
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        plain = await _create(
            client, headers, backend_type="openai_compatible", base_url="http://plain-host:8000"
        )
        fake_backend["plain-host"] = lambda r: httpx.Response(200, json={"data": []})
        down = await _create(client, headers, backend_type="vllm", base_url="http://gone:8000")
        await _service(session, settings).check_all_enabled(load=LEVELS)
        for row in (plain, down):
            count = (
                await session.execute(
                    select(BackendMetricSample).where(
                        BackendMetricSample.backend_id == uuid.UUID(str(row["id"]))
                    )
                )
            ).all()
            assert count == []

    async def test_pruned_and_viewers_may_read(
        self, client: AsyncClient, tokens, backend_admin, backend_viewer, session
    ) -> None:
        created = await _create(client, auth_header(tokens, backend_admin))
        backend_id = uuid.UUID(str(created["id"]))
        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        session.add(BackendMetricSample(backend_id=backend_id, recorded_at=old, values={"x": 1}))
        session.add(BackendMetricSample(backend_id=backend_id, values={"models_loaded": 1}))
        await session.flush()
        deleted = await InferenceBackendRepository(session).prune_samples(
            before=dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
        )
        assert deleted >= 1
        response = await client.get(
            f"/api/v1/backends/{backend_id}/metrics/history?since_minutes=10080",
            headers=auth_header(tokens, backend_viewer),
        )
        assert response.status_code == 200
        assert [s["values"] for s in response.json()["samples"]] == [{"models_loaded": 1.0}]
        # Deleting the backend deletes its samples.
        backend = await session.get(InferenceBackend, backend_id)
        await session.delete(backend)
        await session.flush()
        left = (
            await session.execute(
                select(BackendMetricSample).where(BackendMetricSample.backend_id == backend_id)
            )
        ).all()
        assert left == []


class TestBackendLevels:
    """E90: a backend's own load alert levels, over the platform's."""

    def test_merged(self) -> None:
        own = backend_levels(LEVELS, {"requests_waiting": 50})
        assert own.load_alert_requests_waiting == 50
        assert own.load_alert_kv_cache_percent == 90  # the platform's
        assert backend_levels(LEVELS, None) is LEVELS
        off = backend_levels(LEVELS, {"requests_waiting": 0, "kv_cache_percent": 0})
        assert load_breach({"requests_waiting": 999, "kv_cache_usage_percent": 99}, off) == []

    async def test_a_busy_backend_alerts_later(
        self,
        client: AsyncClient,
        tokens,
        backend_admin,
        backend_viewer,
        fake_backend,
        session,
        settings,
    ) -> None:
        headers = auth_header(tokens, backend_admin)
        created = await _create(
            client, headers, backend_type="vllm", base_url="http://vllm-host:8000"
        )
        backend_id = uuid.UUID(str(created["id"]))
        url = f"/api/v1/backends/{backend_id}/load-alert-levels"

        response = await client.put(
            url, json={"requests_waiting": 20, "kv_cache_percent": 0}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["load_alert_levels"] == {
            "requests_waiting": 20,
            "kv_cache_percent": 0,
        }
        refused = await client.put(
            url, json={"requests_waiting": 1}, headers=auth_header(tokens, backend_viewer)
        )
        assert refused.status_code == 403
        assert (
            await client.put(url, json={"kv_cache_percent": 101}, headers=headers)
        ).status_code == 422

        # 8 waiting and a full cache would alert at the platform's levels, not at its own.
        service = _service(session, settings)
        fake_backend["vllm-host"] = vllm(waiting=8, cache=99)
        await service.check_all_enabled(load=LEVELS)
        await service.check_all_enabled(load=LEVELS)
        assert await _alert(session, backend_id) is None
        fake_backend["vllm-host"] = vllm(waiting=25, cache=99)
        await service.check_all_enabled(load=LEVELS)
        await service.check_all_enabled(load=LEVELS)
        alert = await _alert(session, backend_id)
        assert alert is not None and "25 requests waiting" in alert.title
        assert "KV cache" not in alert.title

        # Back to the platform's.
        response = await client.put(url, json={}, headers=headers)
        assert response.status_code == 200 and response.json()["load_alert_levels"] is None
        listed = await client.get(f"/api/v1/backends/{backend_id}", headers=headers)
        assert listed.json()["load_alert_levels"] is None
