"""GPU probe behaviour (M05)."""

from __future__ import annotations

import pytest

from app.probes import FakeGpuProbe, GpuHealth, NvidiaSmiGpuProbe, classify_health, select_probe
from app.probes.nvidia_smi import _num, _opt_int
from tests.conftest import make_settings


class TestFakeProbe:
    async def test_reports_configured_device_count(self) -> None:
        assert len(await FakeGpuProbe(device_count=8).list_devices()) == 8

    async def test_zero_devices_is_valid(self) -> None:
        """A CPU-only node must be representable."""
        probe = FakeGpuProbe(device_count=0)
        assert await probe.list_devices() == []
        assert await probe.sample_metrics() == []

    async def test_uuids_are_stable_across_instances(self) -> None:
        """The control plane keys GPUs on UUID. A value that changed on restart would
        create duplicate rows every boot and break metric history."""
        first = [d.uuid for d in await FakeGpuProbe(node_name="node-a").list_devices()]
        second = [d.uuid for d in await FakeGpuProbe(node_name="node-a").list_devices()]
        assert first == second

    async def test_uuids_differ_between_nodes(self) -> None:
        """Two fake nodes in one deployment must not collide on GPU identity."""
        a = {d.uuid for d in await FakeGpuProbe(node_name="node-a").list_devices()}
        b = {d.uuid for d in await FakeGpuProbe(node_name="node-b").list_devices()}
        assert a.isdisjoint(b)

    async def test_devices_report_differing_load(self) -> None:
        metrics = await FakeGpuProbe(device_count=4).sample_metrics()
        assert len({m.utilization_percent for m in metrics}) > 1

    async def test_metrics_stay_within_physical_bounds(self) -> None:
        for m in await FakeGpuProbe(device_count=4).sample_metrics():
            assert 0 <= m.utilization_percent <= 100
            assert 0 <= m.memory_used_mib <= m.memory_total_mib
            assert 20 <= m.temperature_celsius <= 95
            assert 0 <= m.power_draw_watts <= m.power_limit_watts
            assert 0 <= m.memory_utilization_percent <= 100

    async def test_nvlink_peers_exclude_self(self) -> None:
        for device in await FakeGpuProbe(device_count=4).list_devices():
            assert device.index not in device.nvlink_peers
            assert len(device.nvlink_peers) == 3

    async def test_reports_no_processes(self) -> None:
        """Process rows reconcile believed placement against actual occupancy;
        fabricating them would make that check always agree with itself."""
        assert await FakeGpuProbe().list_processes() == []

    async def test_driver_info_marks_itself_synthetic(self) -> None:
        info = await FakeGpuProbe().driver_info()
        assert info.probe == "fake"
        assert info.details["synthetic"] == "true"

    async def test_memory_is_reported_in_gib_steps(self) -> None:
        """Real allocators move in large blocks; a smooth curve would hide bugs that
        only appear when memory and utilisation disagree."""
        for m in await FakeGpuProbe(device_count=4).sample_metrics():
            assert m.memory_used_mib % 1024 == 0


class TestHealthClassification:
    def test_healthy_under_normal_conditions(self) -> None:
        assert (
            classify_health(
                temperature_celsius=55,
                memory_used_mib=40000,
                memory_total_mib=81920,
                ecc_errors_uncorrected=0,
            )
            is GpuHealth.HEALTHY
        )

    def test_uncorrectable_ecc_is_critical_regardless(self) -> None:
        """Failing memory makes inference produce wrong answers rather than obvious
        crashes — the worst failure mode this platform has."""
        assert (
            classify_health(
                temperature_celsius=30,
                memory_used_mib=1000,
                memory_total_mib=81920,
                ecc_errors_uncorrected=1,
            )
            is GpuHealth.CRITICAL
        )

    @pytest.mark.parametrize(
        ("temperature", "expected"),
        [(82, GpuHealth.HEALTHY), (85, GpuHealth.WARNING), (91, GpuHealth.CRITICAL)],
    )
    def test_temperature_thresholds(self, temperature: float, expected: GpuHealth) -> None:
        assert (
            classify_health(
                temperature_celsius=temperature,
                memory_used_mib=1000,
                memory_total_mib=81920,
                ecc_errors_uncorrected=0,
            )
            is expected
        )

    def test_near_full_memory_warns(self) -> None:
        """Not an error, but the next deployment onto this GPU will OOM."""
        assert (
            classify_health(
                temperature_celsius=50,
                memory_used_mib=81000,
                memory_total_mib=81920,
                ecc_errors_uncorrected=0,
            )
            is GpuHealth.WARNING
        )

    def test_unknown_ecc_support_does_not_trigger_critical(self) -> None:
        """None means "this device does not report ECC", not "zero errors"."""
        assert (
            classify_health(
                temperature_celsius=50,
                memory_used_mib=1000,
                memory_total_mib=81920,
                ecc_errors_uncorrected=None,
            )
            is GpuHealth.HEALTHY
        )


class TestNvidiaSmiParsing:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [("42", 42.0), (" 3.5 ", 3.5), ("[N/A]", 0.0), ("[Not Supported]", 0.0), ("", 0.0)],
    )
    def test_numeric_parsing_tolerates_unsupported_fields(
        self, token: str, expected: float
    ) -> None:
        assert _num(token) == expected

    @pytest.mark.parametrize(
        ("token", "expected"),
        [("7", 7), ("0", 0), ("[N/A]", None), ("[Not Supported]", None), ("garbage", None)],
    )
    def test_optional_int_distinguishes_zero_from_unsupported(
        self, token: str, expected: int | None
    ) -> None:
        """A consumer GPU with no ECC support must not be recorded as a device
        reporting zero ECC errors."""
        assert _opt_int(token) == expected

    async def test_unavailable_when_binary_is_missing(self) -> None:
        probe = NvidiaSmiGpuProbe(binary="definitely-not-a-real-binary")
        assert await probe.is_available() is False


class TestProbeSelection:
    async def test_explicit_fake_is_honoured(self) -> None:
        probe = await select_probe(make_settings(gpu_probe="fake"))
        assert probe.name == "fake"

    async def test_explicit_choice_is_not_silently_downgraded(self) -> None:
        """If an operator asks for DCGM and DCGM is broken, that must surface as a
        visible failure. Reporting fake GPUs on a real GPU host would be far worse
        than reporting none."""
        probe = await select_probe(make_settings(gpu_probe="dcgm"))
        assert probe.name == "dcgm"

        probe = await select_probe(make_settings(gpu_probe="nvidia_smi"))
        assert probe.name == "nvidia_smi"

    async def test_auto_falls_back_to_fake_without_hardware(self) -> None:
        """This is what makes a developer machine behave like a GPU node."""
        probe = await select_probe(make_settings(gpu_probe="auto"))
        assert probe.name == "fake"

    async def test_auto_respects_configured_fake_shape(self) -> None:
        probe = await select_probe(make_settings(gpu_probe="auto", fake_device_count=2))
        assert len(await probe.list_devices()) == 2
