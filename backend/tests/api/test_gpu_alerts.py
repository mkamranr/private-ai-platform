"""GPU threshold alerts (E52).

The watcher is driven directly against real Postgres. Each test hides GPU alerts other
processes committed (the delete rolls back with the test) and works on its own node.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import GpuSettings
from app.models.infrastructure import Gpu, GpuMetric, Node, NodeStatus
from app.models.operations import Alert, AlertSeverity, AlertState
from app.repositories.audit import AuditRepository
from app.repositories.infrastructure import GpuMetricRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.gpu_alerts import (
    GPU_ALERT_SOURCE,
    GpuAlertWatcher,
    evaluate,
    gpu_alert_fingerprint,
)
from app.services.notifications import KNOWN_SOURCES
from tests.conftest import auth_header

NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
async def _hide_other_gpu_alerts(session: AsyncSession) -> None:
    await session.execute(delete(Alert).where(Alert.source == GPU_ALERT_SOURCE))
    await session.execute(delete(GpuMetric))
    await session.flush()


@pytest.fixture
async def gpu(session: AsyncSession) -> Gpu:
    node = Node(
        name=f"gpu-node-{uuid.uuid4().hex[:6]}",
        agent_url="http://agent:9100",
        agent_token_encrypted="x",
        status=NodeStatus.ONLINE,
    )
    session.add(node)
    await session.flush()
    card = Gpu(
        node_id=node.id,
        index=2,
        uuid=f"GPU-{uuid.uuid4()}",
        name="NVIDIA A100-SXM4-80GB",
        memory_total_mib=81920,
    )
    session.add(card)
    await session.flush()
    return card


async def _sample(
    session: AsyncSession,
    gpu: Gpu,
    *,
    temperature: float = 60,
    memory_used: int = 40000,
    ecc: int | None = 0,
    at: dt.datetime = NOW,
) -> None:
    session.add(
        GpuMetric(
            gpu_id=gpu.id,
            recorded_at=at,
            utilization_percent=50,
            memory_used_mib=memory_used,
            memory_total_mib=81920,
            temperature_celsius=temperature,
            power_draw_watts=250,
            ecc_errors_uncorrected=ecc,
        )
    )
    await session.flush()


def _watcher(session: AsyncSession, clock: dt.datetime = NOW) -> GpuAlertWatcher:
    repo = AlertRepository(session)
    return GpuAlertWatcher(
        GpuMetricRepository(session),
        AlertService(repo, AuditService(AuditRepository(session))),
        repo,
        clock=lambda: clock,
    )


async def _open_alert(session: AsyncSession, gpu: Gpu) -> Alert | None:
    return (
        await session.execute(
            select(Alert).where(
                Alert.fingerprint == gpu_alert_fingerprint(gpu.id),
                Alert.state != AlertState.RESOLVED,
            )
        )
    ).scalar_one_or_none()


class TestEvaluate:
    def _metric(self, **kw) -> GpuMetric:
        values = {
            "temperature_celsius": 60.0,
            "memory_used_mib": 1000,
            "memory_total_mib": 81920,
            "ecc_errors_uncorrected": None,
        } | kw
        return GpuMetric(**values)

    def test_within_thresholds_is_quiet(self) -> None:
        assert evaluate(self._metric(), GpuSettings()) is None

    def test_levels_and_worst_wins(self) -> None:
        settings = GpuSettings()
        assert evaluate(self._metric(temperature_celsius=83), settings).severity == "WARNING"
        assert evaluate(self._metric(temperature_celsius=90), settings).severity == "CRITICAL"
        breach = evaluate(self._metric(temperature_celsius=85, ecc_errors_uncorrected=2), settings)
        assert breach.severity == AlertSeverity.CRITICAL
        assert breach.reasons == ("2 uncorrectable ECC errors", "85 °C")

    def test_memory_warning(self) -> None:
        breach = evaluate(self._metric(memory_used_mib=81000), GpuSettings())
        assert breach.severity == AlertSeverity.WARNING
        assert breach.reasons == ("memory 99% used",)

    def test_unknown_ecc_is_not_an_error(self) -> None:
        assert evaluate(self._metric(ecc_errors_uncorrected=None), GpuSettings()) is None

    def test_warning_must_be_below_critical(self) -> None:
        with pytest.raises(ValueError, match="below the critical"):
            GpuSettings(alert_temperature_warning_celsius=90, alert_temperature_critical_celsius=90)


class TestWatcher:
    async def test_warning_then_critical_then_cleared(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        settings = GpuSettings()
        await _sample(session, gpu, temperature=85)
        result = await _watcher(session).run(settings)
        assert result.raised == [gpu_alert_fingerprint(gpu.id)]
        alert = await _open_alert(session, gpu)
        assert alert is not None and alert.severity == AlertSeverity.WARNING
        assert alert.title.startswith("GPU 2 on gpu-node-") and alert.title.endswith("85 °C")
        assert alert.resource_type == "gpu" and alert.team_id is None

        await _sample(session, gpu, temperature=93, at=NOW + dt.timedelta(seconds=15))
        await _watcher(session, NOW + dt.timedelta(seconds=15)).run(settings)
        await session.refresh(alert)
        assert alert.severity == AlertSeverity.CRITICAL and alert.occurrences == 2

        await _sample(session, gpu, temperature=70, at=NOW + dt.timedelta(seconds=30))
        result = await _watcher(session, NOW + dt.timedelta(seconds=30)).run(settings)
        assert result.cleared == [gpu_alert_fingerprint(gpu.id)]
        assert await _open_alert(session, gpu) is None

    async def test_ecc_errors_are_critical(self, session: AsyncSession, gpu: Gpu) -> None:
        await _sample(session, gpu, ecc=1)
        await _watcher(session).run(GpuSettings())
        alert = await _open_alert(session, gpu)
        assert alert.severity == AlertSeverity.CRITICAL
        assert "1 uncorrectable ECC error" in alert.title

    async def test_a_gpu_that_stops_reporting_is_cleared(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        await _sample(session, gpu, temperature=95)
        await _watcher(session).run(GpuSettings())
        assert await _open_alert(session, gpu) is not None

        later = NOW + dt.timedelta(minutes=10)
        await _watcher(session, later).run(GpuSettings())
        assert await _open_alert(session, gpu) is None

    async def test_turning_alerts_off_clears_them(self, session: AsyncSession, gpu: Gpu) -> None:
        await _sample(session, gpu, temperature=95)
        await _watcher(session).run(GpuSettings())
        result = await _watcher(session).run(GpuSettings(alerts_enabled=False))
        assert result.cleared == [gpu_alert_fingerprint(gpu.id)]

    async def test_thresholds_are_configurable(self, session: AsyncSession, gpu: Gpu) -> None:
        await _sample(session, gpu, temperature=75, memory_used=70000)
        assert (await _watcher(session).run(GpuSettings())).raised == []
        settings = GpuSettings(
            alert_temperature_warning_celsius=70,
            alert_temperature_critical_celsius=80,
            alert_memory_warning_percent=85,
        )
        await _watcher(session).run(settings)
        alert = await _open_alert(session, gpu)
        assert alert.severity == AlertSeverity.WARNING
        assert alert.title.endswith("75 °C, memory 85% used")

    async def test_only_the_latest_sample_counts(self, session: AsyncSession, gpu: Gpu) -> None:
        await _sample(session, gpu, temperature=95, at=NOW - dt.timedelta(seconds=30))
        await _sample(session, gpu, temperature=60)
        assert (await _watcher(session).run(GpuSettings())).raised == []


class TestSettingsAndChannels:
    def test_channels_can_filter_on_gpu_alerts(self) -> None:
        assert GPU_ALERT_SOURCE in KNOWN_SOURCES

    async def test_thresholds_are_runtime_settings(
        self, client: AsyncClient, tokens, superuser
    ) -> None:
        headers = auth_header(tokens, superuser)
        response = await client.put(
            "/api/v1/settings/gpu.alert_temperature_warning_celsius",
            json={"value": 95, "reason": "test"},
            headers=headers,
        )
        assert response.status_code == 422, response.text
        assert "below the critical" in response.json()["error"]["message"]

        response = await client.put(
            "/api/v1/settings/gpu.alert_memory_warning_percent",
            json={"value": 92, "reason": "test"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
