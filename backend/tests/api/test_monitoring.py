"""Metrics, the monitoring overview and traces (M19, Phase 7).

Weighted towards the two things that go wrong quietly.

**Label cardinality.** A metric labelled with a path that contains an id adds a time
series per resource, for ever, and nothing fails — the platform keeps serving while
Prometheus's memory climbs. So the test asserts the *template* is the label.

**Reachability.** `/metrics` is unauthenticated by design and kept private by the
network. A change that moves it under the API prefix, or puts a permission on it, breaks
scraping in one direction or exposes the platform's internals in the other. Both are
asserted here rather than left to nginx alone.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.permissions import Permission as Perm
from app.models.agents import Agent, AgentRun, AgentRunEvent, AgentVersion, RunState
from app.models.auth import User
from tests.api.conftest import _user_with
from tests.conftest import auth_header


@pytest.fixture
async def observer(session: AsyncSession, settings) -> User:
    return await _user_with(session, settings, [Perm.MONITORING_VIEW, Perm.TRACE_VIEW])


@pytest.fixture
async def watcher_only(session: AsyncSession, settings) -> User:
    """Can see the overview but not the traces — the split that matters (see the router)."""
    return await _user_with(session, settings, [Perm.MONITORING_VIEW])


@pytest.fixture
async def traced_run(session: AsyncSession) -> AgentRun:
    agent = Agent(slug="trace-agent", display_name="Trace Agent", enabled=True)
    session.add(agent)
    await session.flush()

    version = AgentVersion(
        agent_id=agent.id,
        version=1,
        system_prompt="you are helpful",
        model="enterprise-chat",
    )
    session.add(version)
    await session.flush()

    run = AgentRun(
        agent_id=agent.id,
        agent_version_id=version.id,
        trace_id="a" * 32,
        state=RunState.COMPLETED,
        input="why is ABC123 locked out?",
        output="the account is locked",
        iterations=2,
        prompt_tokens=120,
        completion_tokens=45,
        user_permissions=[],
    )
    session.add(run)
    await session.flush()

    session.add_all(
        [
            AgentRunEvent(run_id=run.id, sequence=1, type="run_started", payload={}),
            AgentRunEvent(run_id=run.id, sequence=2, type="llm_call", payload={"model": "x"}),
        ]
    )
    await session.flush()
    return run


class TestMetricsEndpoint:
    async def test_metrics_are_exposed_without_a_token(self, client: AsyncClient) -> None:
        """Unauthenticated on purpose: a scraper holds no JWT.

        What keeps it private is the network — nginx does not proxy it. If this ever
        starts requiring a token, scraping breaks silently and the dashboards go flat.
        """
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "ai_platform_http_requests_total" in response.text

    async def test_metrics_are_not_under_the_api_prefix(self, client: AsyncClient) -> None:
        """It must not sit under the prefix nginx proxies to the world."""
        assert (await client.get("/api/v1/metrics")).status_code == 404

    async def test_exposition_format_matches_the_declared_content_type(
        self, client: AsyncClient
    ) -> None:
        """The bug this test exists for returned 200 with a body full of metrics.

        Declaring the OpenMetrics content type while writing the Prometheus text format
        makes Prometheus reject every scrape with `data does not end with # EOF`. The
        endpoint looks perfect to curl; the target just reports `up 0` for ever. So
        assert the pair agrees rather than that the body merely contains a metric.
        """
        response = await client.get("/metrics")
        content_type = response.headers["content-type"]
        assert content_type.startswith("text/plain")
        assert "openmetrics" not in content_type
        # The text format has no terminator; OpenMetrics requires one. Either is fine,
        # but they must match the header above.
        assert not response.text.rstrip().endswith("# EOF")

    async def test_build_info_carries_the_version(self, client: AsyncClient) -> None:
        response = await client.get("/metrics")
        assert "ai_platform_build_info" in response.text

    async def test_requests_are_labelled_by_route_template_not_by_path(
        self, client: AsyncClient, observer: User, tokens
    ) -> None:
        """The cardinality guard, and the whole reason this test file exists.

        A request to a path containing a UUID must produce a series labelled with the
        template. If the raw path leaks into the label, every id ever requested becomes
        a permanent time series.
        """
        unknown = uuid.uuid4().hex
        await client.get(f"/api/v1/traces/{unknown}", headers=auth_header(tokens, observer))

        body = (await client.get("/metrics")).text
        assert unknown not in body, "a resource id reached a metric label"
        assert "/api/v1/traces/{trace_id}" in body

    async def test_an_unmatched_path_cannot_create_series_at_will(
        self, client: AsyncClient
    ) -> None:
        """A 404 scan must collapse to one label, not one per probed path."""
        for path in ("/wp-login.php", "/.env", "/admin.php"):
            await client.get(path)

        body = (await client.get("/metrics")).text
        assert "wp-login" not in body
        assert 'route="unmatched"' in body

    async def test_status_is_a_class_not_a_code(self) -> None:
        assert metrics.status_class(204) == "2xx"
        assert metrics.status_class(404) == "4xx"
        assert metrics.status_class(503) == "5xx"


class TestMonitoringOverview:
    async def test_overview_requires_the_permission(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/monitoring/overview")).status_code == 401

    async def test_overview_reports_which_collectors_are_deployed(
        self, client: AsyncClient, observer: User, tokens
    ) -> None:
        """The answer to "why is Grafana empty" is meant to be visible here."""
        response = await client.get(
            "/api/v1/monitoring/overview", headers=auth_header(tokens, observer)
        )
        assert response.status_code == 200
        collectors = response.json()["collectors"]
        assert collectors["metrics"]["enabled"] is True
        # Off by default: a site that never started the monitoring profile must not be
        # told it has tracing.
        assert collectors["tracing"]["enabled"] is False
        assert collectors["langfuse"]["enabled"] is False

    async def test_failure_rate_is_zero_not_an_error_on_a_quiet_platform(
        self, client: AsyncClient, observer: User, tokens
    ) -> None:
        """Nobody should get a 500 out of a division by zero on an idle install.

        The guard is `failed / runs if runs else 0.0`, so the case worth pinning is
        `runs == 0`. That is asserted when the window is genuinely empty, and this reads
        the platform's real tables rather than a fixture: anyone who has executed an agent
        in the last hour makes the window non-empty, at which point a flat
        `failure_rate == 0.0` is asserting that the platform is idle, not that the guard
        works.
        """
        response = await client.get(
            "/api/v1/monitoring/overview?window_hours=1", headers=auth_header(tokens, observer)
        )
        assert response.status_code == 200
        agents = response.json()["agents"]
        rate = agents["failure_rate"]

        if agents["runs"] == 0:
            assert rate == 0.0, "division by zero must yield 0.0, not an error or a null"
        else:
            # Not idle, so the guard is not the thing under test — but the endpoint must
            # still answer with a rate that is a real proportion.
            assert isinstance(rate, int | float)
            assert 0.0 <= rate <= 1.0


class TestTraces:
    async def test_a_trace_resolves_to_its_run_and_events(
        self, client: AsyncClient, observer: User, tokens, traced_run: AgentRun
    ) -> None:
        response = await client.get(
            f"/api/v1/traces/{'a' * 32}", headers=auth_header(tokens, observer)
        )
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == str(traced_run.id)
        assert body["agent_slug"] == "trace-agent"
        assert [e["type"] for e in body["events"]] == ["run_started", "llm_call"]
        assert body["prompt_tokens"] == 120

    async def test_no_tempo_link_when_tracing_is_off(
        self, client: AsyncClient, observer: User, tokens, traced_run: AgentRun
    ) -> None:
        """A deep link into a Tempo that was never deployed is a broken link."""
        response = await client.get(
            f"/api/v1/traces/{'a' * 32}", headers=auth_header(tokens, observer)
        )
        assert response.json()["tempo_url"] is None

    async def test_an_unknown_trace_is_404_not_500(
        self, client: AsyncClient, observer: User, tokens
    ) -> None:
        response = await client.get(
            f"/api/v1/traces/{'b' * 32}", headers=auth_header(tokens, observer)
        )
        assert response.status_code == 404

    async def test_monitoring_view_alone_cannot_read_a_trace(
        self, client: AsyncClient, watcher_only: User, tokens, traced_run: AgentRun
    ) -> None:
        """A trace carries the user's prompt and the agent's answer.

        Someone entitled to watch load graphs is not thereby entitled to read what
        people asked the agents — so `trace.view` is a separate permission, and this is
        the test that keeps it separate.
        """
        response = await client.get(
            f"/api/v1/traces/{'a' * 32}", headers=auth_header(tokens, watcher_only)
        )
        assert response.status_code == 403
