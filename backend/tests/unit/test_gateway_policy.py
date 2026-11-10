"""Circuit breakers and budget windows (E2) — pure logic, no services."""

from __future__ import annotations

import datetime as dt

from app.config.settings import GatewaySettings
from app.services.gateway_policy import CircuitBreakers, _keys, _next_month, _start_of_month


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


SETTINGS = GatewaySettings(
    circuit_failure_threshold=3, circuit_window_seconds=60, circuit_open_seconds=30
)


def test_opens_after_threshold_failures_within_window() -> None:
    clock = Clock()
    breakers = CircuitBreakers(clock)
    assert breakers.record_failure("b", SETTINGS) is False
    assert breakers.record_failure("b", SETTINGS) is False
    assert breakers.open_for("b") is None
    assert breakers.record_failure("b", SETTINGS, "boom") is True
    assert breakers.open_for("b") == 30


def test_failures_outside_the_window_do_not_count() -> None:
    clock = Clock()
    breakers = CircuitBreakers(clock)
    breakers.record_failure("b", SETTINGS)
    breakers.record_failure("b", SETTINGS)
    clock.now += 61
    assert breakers.record_failure("b", SETTINGS) is False
    assert breakers.open_for("b") is None


def test_half_open_trial_failure_reopens_immediately() -> None:
    clock = Clock()
    breakers = CircuitBreakers(clock)
    for _ in range(3):
        breakers.record_failure("b", SETTINGS)
    clock.now += 31
    # Cool-down over: one trial is allowed through...
    assert breakers.open_for("b") is None
    assert breakers.snapshot()[0]["state"] == "half_open"
    # ...and a single failure re-opens it, without needing the full threshold again.
    clock.now += 100
    assert breakers.record_failure("b", SETTINGS) is True
    assert breakers.open_for("b") == 30


def test_success_closes_and_forgets() -> None:
    clock = Clock()
    breakers = CircuitBreakers(clock)
    for _ in range(3):
        breakers.record_failure("b", SETTINGS)
    clock.now += 31
    breakers.open_for("b")
    breakers.record_success("b")
    assert breakers.snapshot() == []
    # A fresh run of failures is needed to open it again.
    assert breakers.record_failure("b", SETTINGS) is False


def test_circuits_are_independent() -> None:
    breakers = CircuitBreakers(Clock())
    for _ in range(3):
        breakers.record_failure("a", SETTINGS)
    assert breakers.open_for("a") is not None
    assert breakers.open_for("b") is None


def test_window_keys_are_utc_calendar_windows() -> None:
    now = dt.datetime(2026, 12, 31, 23, 59, 30, tzinfo=dt.UTC)
    keys = _keys("api_key", "k1", now)
    assert keys["day"] == "gw:day:api_key:k1:20261231"
    assert keys["month"] == "gw:month:api_key:k1:202612"
    assert keys["minute"].startswith("gw:tpm:api_key:k1:")


def test_month_boundaries_roll_over_the_year() -> None:
    now = dt.datetime(2026, 12, 15, 8, 0, tzinfo=dt.UTC)
    assert _start_of_month(now) == dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
    assert _next_month(now) == dt.datetime(2027, 1, 1, tzinfo=dt.UTC)
    assert _next_month(dt.datetime(2026, 1, 31, tzinfo=dt.UTC)) == dt.datetime(
        2026, 2, 1, tzinfo=dt.UTC
    )
