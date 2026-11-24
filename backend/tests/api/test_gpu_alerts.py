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
from app.models.audit import AuditLog
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
    node_settings,
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
    replays: int | None = None,
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
            pcie_replay_counter=replays,
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


class TestNoFlapping:
    """E68: a breach has to last before it alerts, and has to clearly end before it clears."""

    @staticmethod
    def _at(minutes: float) -> dt.datetime:
        return NOW + dt.timedelta(minutes=minutes)

    async def test_a_short_spike_stays_quiet(self, session: AsyncSession, gpu: Gpu) -> None:
        settings = GpuSettings(alert_sustain_minutes=3)
        fingerprint = gpu_alert_fingerprint(gpu.id)
        await _sample(session, gpu, temperature=60, at=self._at(-1))
        for minute in (0, 1, 2):
            await _sample(session, gpu, temperature=86, at=self._at(minute))
            result = await _watcher(session, self._at(minute)).run(settings)
            assert result.pending == [fingerprint] and result.raised == []
        await _sample(session, gpu, temperature=86, at=self._at(3))
        result = await _watcher(session, self._at(3)).run(settings)
        assert result.raised == [fingerprint]

    async def test_a_dip_starts_the_count_again(self, session: AsyncSession, gpu: Gpu) -> None:
        settings = GpuSettings(alert_sustain_minutes=2)
        await _sample(session, gpu, temperature=86, at=self._at(0))
        await _sample(session, gpu, temperature=80, at=self._at(1))
        await _sample(session, gpu, temperature=86, at=self._at(2))
        assert (await _watcher(session, self._at(2)).run(settings)).raised == []
        await _sample(session, gpu, temperature=91, at=self._at(4))
        result = await _watcher(session, self._at(4)).run(settings)
        assert result.raised == [gpu_alert_fingerprint(gpu.id)]
        assert (await _open_alert(session, gpu)).severity == AlertSeverity.CRITICAL

    async def test_a_gap_in_the_samples_starts_the_count_again(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        settings = GpuSettings(alert_sustain_minutes=5)
        await _sample(session, gpu, temperature=86, at=self._at(0))
        await _sample(session, gpu, temperature=86, at=self._at(6))
        assert (await _watcher(session, self._at(6)).run(settings)).raised == []

    async def test_ecc_errors_raise_at_once(self, session: AsyncSession, gpu: Gpu) -> None:
        await _sample(session, gpu, ecc=2)
        result = await _watcher(session).run(GpuSettings(alert_sustain_minutes=10))
        assert result.raised == [gpu_alert_fingerprint(gpu.id)]

    async def test_an_open_alert_needs_the_margin_to_clear(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        settings = GpuSettings(alert_recovery_margin_celsius=3)
        await _sample(session, gpu, temperature=84, at=self._at(0))
        await _watcher(session, self._at(0)).run(settings)
        assert await _open_alert(session, gpu) is not None

        # 81 °C is under the 83 °C line but inside the margin: still open.
        await _sample(session, gpu, temperature=81, at=self._at(1))
        result = await _watcher(session, self._at(1)).run(settings)
        assert result.cleared == [] and await _open_alert(session, gpu) is not None

        await _sample(session, gpu, temperature=79, at=self._at(2))
        result = await _watcher(session, self._at(2)).run(settings)
        assert result.cleared == [gpu_alert_fingerprint(gpu.id)]

        # Closed again, so 82 °C (inside the margin) does not reopen it.
        await _sample(session, gpu, temperature=82, at=self._at(3))
        assert (await _watcher(session, self._at(3)).run(settings)).raised == []

    async def test_critical_drops_to_warning_only_past_the_margin(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        settings = GpuSettings(alert_recovery_margin_celsius=3)
        await _sample(session, gpu, temperature=91, at=self._at(0))
        await _watcher(session, self._at(0)).run(settings)
        await _sample(session, gpu, temperature=88, at=self._at(1))
        await _watcher(session, self._at(1)).run(settings)
        alert = await _open_alert(session, gpu)
        assert alert.severity == AlertSeverity.CRITICAL
        await _sample(session, gpu, temperature=86, at=self._at(2))
        await _watcher(session, self._at(2)).run(settings)
        await session.refresh(alert)
        assert alert.severity == AlertSeverity.WARNING

    async def test_memory_has_its_own_margin(self, session: AsyncSession, gpu: Gpu) -> None:
        settings = GpuSettings(alert_memory_warning_percent=90, alert_recovery_margin_percent=5)
        await _sample(session, gpu, memory_used=75000, at=self._at(0))  # 91.6%
        await _watcher(session, self._at(0)).run(settings)
        await _sample(session, gpu, memory_used=71000, at=self._at(1))  # 86.7%
        assert (await _watcher(session, self._at(1)).run(settings)).cleared == []
        await _sample(session, gpu, memory_used=69000, at=self._at(2))  # 84.2%
        assert (await _watcher(session, self._at(2)).run(settings)).cleared != []

    async def test_the_margins_are_runtime_settings(
        self, client: AsyncClient, tokens, superuser
    ) -> None:
        headers = auth_header(tokens, superuser)
        for key, value in (
            ("gpu.alert_sustain_minutes", 5),
            ("gpu.alert_recovery_margin_celsius", 4),
            ("gpu.alert_recovery_margin_percent", 3),
        ):
            response = await client.put(
                f"/api/v1/settings/{key}", json={"value": value, "reason": "test"}, headers=headers
            )
            assert response.status_code == 200, response.text
        response = await client.put(
            "/api/v1/settings/gpu.alert_sustain_minutes",
            json={"value": 120, "reason": "test"},
            headers=headers,
        )
        assert response.status_code == 422


class TestNodeLevels:
    """E70: a node's own levels replace the platform's for its GPUs."""

    async def _node_levels(self, session: AsyncSession, gpu: Gpu, **levels) -> None:
        node = await session.get(Node, gpu.node_id)
        node.gpu_alert_thresholds = levels
        await session.flush()

    async def test_a_hot_room_node_alerts_later(self, session: AsyncSession, gpu: Gpu) -> None:
        await self._node_levels(
            session, gpu, temperature_warning_celsius=88, temperature_critical_celsius=95
        )
        await _sample(session, gpu, temperature=86)
        assert (await _watcher(session).run(GpuSettings())).raised == []
        await _sample(session, gpu, temperature=91, at=NOW + dt.timedelta(seconds=15))
        await _watcher(session, NOW + dt.timedelta(seconds=15)).run(GpuSettings())
        alert = await _open_alert(session, gpu)
        assert alert.severity == AlertSeverity.WARNING  # 91 < this node's critical 95
        assert "warning level (88 °C)" in alert.message

    async def test_unset_levels_come_from_the_platform(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        await self._node_levels(session, gpu, memory_warning_percent=80)
        await _sample(session, gpu, memory_used=70000)  # 85%
        await _watcher(session).run(GpuSettings())
        alert = await _open_alert(session, gpu)
        assert alert.severity == AlertSeverity.WARNING and "memory 85% used" in alert.title
        await _sample(session, gpu, temperature=91, at=NOW + dt.timedelta(seconds=15))
        await _watcher(session, NOW + dt.timedelta(seconds=15)).run(GpuSettings())
        await session.refresh(alert)
        assert alert.severity == AlertSeverity.CRITICAL  # the platform's 90 °C

    def test_levels_that_no_longer_fit_fall_back(self) -> None:
        platform = GpuSettings(alert_temperature_critical_celsius=85)
        merged = node_settings(platform, {"temperature_warning_celsius": 88})
        assert merged == platform
        merged = node_settings(GpuSettings(), {"temperature_warning_celsius": 70, "x": 1})
        assert merged.alert_temperature_warning_celsius == 70

    async def test_set_through_the_api(
        self, client: AsyncClient, tokens, superuser, gpu: Gpu, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        url = f"/api/v1/nodes/{gpu.node_id}/gpu-alert-levels"
        response = await client.put(url, json={"temperature_warning_celsius": 92}, headers=headers)
        assert response.status_code == 422, response.text  # above the platform's 90 °C critical
        assert "below the critical" in response.json()["error"]["message"]

        response = await client.put(
            url,
            json={"temperature_warning_celsius": 88, "temperature_critical_celsius": 95},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["gpu_alert_thresholds"] == {
            "temperature_warning_celsius": 88,
            "temperature_critical_celsius": 95,
        }
        action = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == str(gpu.node_id))
                )
            )
            .scalars()
            .all()
        )
        assert "NODE_GPU_LEVELS_CHANGED" in action

        response = await client.put(url, json={}, headers=headers)
        assert response.status_code == 200 and response.json()["gpu_alert_thresholds"] is None


class TestPcieReplays:
    """E87: a WARNING when the PCIe replay counter climbs fast."""

    @staticmethod
    def _at(minutes: float) -> dt.datetime:
        return NOW + dt.timedelta(minutes=minutes)

    def test_evaluate(self) -> None:
        metric = GpuMetric(temperature_celsius=60.0, memory_used_mib=1, memory_total_mib=10)
        settings = GpuSettings(alert_pcie_replays_per_hour=100)
        assert evaluate(metric, settings, pcie_replays=99) is None
        breach = evaluate(metric, settings, pcie_replays=150)
        assert breach.severity == AlertSeverity.WARNING
        assert breach.reasons == ("150 PCIe replays in an hour",)
        assert evaluate(metric, GpuSettings(), pcie_replays=10**6) is None  # off by default

    async def test_growth_over_the_hour_raises_at_once_and_clears(
        self, session: AsyncSession, gpu: Gpu
    ) -> None:
        settings = GpuSettings(alert_pcie_replays_per_hour=100, alert_sustain_minutes=5)
        fingerprint = gpu_alert_fingerprint(gpu.id)
        await _sample(session, gpu, replays=5000, at=self._at(-90))  # outside the hour
        await _sample(session, gpu, replays=10_000, at=self._at(-50))
        await _sample(session, gpu, replays=10_050, at=self._at(-1))
        assert (await _watcher(session, self._at(0)).run(settings)).raised == []
        await _sample(session, gpu, replays=10_200, at=self._at(0))
        result = await _watcher(session, self._at(0)).run(settings)
        assert result.raised == [fingerprint]  # no sustain wait
        assert "PCIe replays" in (await _open_alert(session, gpu)).title
        # An hour later the counter has stopped climbing.
        await _sample(session, gpu, replays=10_200, at=self._at(61))
        result = await _watcher(session, self._at(61)).run(settings)
        assert result.cleared == [fingerprint]

    async def test_a_counter_reset_is_not_negative(self, session: AsyncSession, gpu: Gpu) -> None:
        repo = GpuMetricRepository(session)
        await _sample(session, gpu, replays=900, at=self._at(-30))
        await _sample(session, gpu, replays=40, at=self._at(-20))  # driver reset
        await _sample(session, gpu, replays=70, at=self._at(-10))
        growth = await repo.pcie_replay_growth([gpu.id], self._at(-60))
        assert growth == {gpu.id: 70}
