"""E75: per-GPU alert levels, utilisation and power alerts, and falling faithfulness."""

# ruff: noqa: F811  (pytest fixtures imported from the E52 tests)
from __future__ import annotations

import datetime as dt

from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import GpuSettings, Settings
from app.models.agents import Agent, AgentAnswerScore, AnswerScoreStatus
from app.models.infrastructure import Gpu, GpuMetric, Node
from app.models.operations import Alert, AlertSeverity, AlertState
from app.repositories.agents import AgentAnswerScoreRepository
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.answer_scoring import FAITHFULNESS_ALERT_SOURCE, AnswerScoringService
from app.services.audit import AuditService
from app.services.gpu_alerts import evaluate, merged_levels
from tests.api.test_agents import _make_agent, _make_run
from tests.api.test_gpu_alerts import (  # noqa: F401  (fixtures)
    NOW,
    _hide_other_gpu_alerts,
    _open_alert,
    _watcher,
    gpu,
)
from tests.conftest import auth_header


async def sample(
    session: AsyncSession,
    card: Gpu,
    *,
    temperature: float = 60,
    utilization: float = 50,
    power: float = 250,
    limit: float | None = 400,
    at: dt.datetime = NOW,
) -> None:
    session.add(
        GpuMetric(
            gpu_id=card.id,
            recorded_at=at,
            utilization_percent=utilization,
            memory_used_mib=40000,
            memory_total_mib=81920,
            temperature_celsius=temperature,
            power_draw_watts=power,
            power_limit_watts=limit,
            ecc_errors_uncorrected=0,
        )
    )
    await session.flush()


def metric(**values) -> GpuMetric:
    base = {
        "utilization_percent": 50,
        "memory_used_mib": 40000,
        "memory_total_mib": 81920,
        "temperature_celsius": 60,
        "power_draw_watts": 250,
        "power_limit_watts": 400,
        "ecc_errors_uncorrected": 0,
    }
    return GpuMetric(**{**base, **values})


class TestUtilisationAndPower:
    def test_off_by_default(self) -> None:
        assert (
            evaluate(metric(utilization_percent=100, power_draw_watts=400), GpuSettings()) is None
        )

    def test_levels(self) -> None:
        settings = GpuSettings(alert_utilization_warning_percent=95, alert_power_warning_percent=90)
        breach = evaluate(metric(utilization_percent=97, power_draw_watts=380), settings)
        assert breach.severity == AlertSeverity.WARNING
        assert breach.reasons == ("97% busy", "power 95% of limit")
        assert evaluate(metric(utilization_percent=90), settings) is None
        # No reported limit, no power check.
        assert evaluate(metric(power_draw_watts=500, power_limit_watts=None), settings) is None

    def test_margin_while_open(self) -> None:
        settings = GpuSettings(alert_utilization_warning_percent=95)
        assert evaluate(metric(utilization_percent=94), settings) is None
        assert evaluate(metric(utilization_percent=94), settings, recovering=True) is not None


class TestGpuLevels:
    def test_a_gpus_levels_win_key_by_key(self) -> None:
        merged = merged_levels(
            {"temperature_warning_celsius": 85, "memory_warning_percent": 90},
            {"temperature_warning_celsius": 80, "memory_warning_percent": None},
        )
        assert merged == {"temperature_warning_celsius": 80, "memory_warning_percent": 90}

    async def test_one_hot_card_alerts_earlier(self, session: AsyncSession, gpu: Gpu) -> None:
        node = await session.get(Node, gpu.node_id)
        node.gpu_alert_thresholds = {"temperature_warning_celsius": 86}
        gpu.alert_thresholds = {"temperature_warning_celsius": 75, "utilization_warning_percent": 0}
        await session.flush()
        await sample(session, gpu, temperature=78)
        await _watcher(session).run(GpuSettings(alert_utilization_warning_percent=40))
        alert = await _open_alert(session, gpu)
        # 78 °C is over this card's 75, and its 0 turns the platform's utilisation check off.
        assert alert.severity == AlertSeverity.WARNING
        assert alert.title.endswith("78 °C")

    async def test_set_through_the_api(
        self, client: AsyncClient, tokens, superuser, gpu: Gpu, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        node = await session.get(Node, gpu.node_id)
        node.gpu_alert_thresholds = {"temperature_critical_celsius": 85}
        await session.flush()
        url = f"/api/v1/gpus/{gpu.id}/alert-levels"
        # Over the node's critical, which it would fall back to.
        response = await client.put(url, json={"temperature_warning_celsius": 86}, headers=headers)
        assert response.status_code == 422 and "node's" in response.text
        response = await client.put(
            url,
            json={"temperature_warning_celsius": 80, "power_warning_percent": 95},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["alert_thresholds"] == {
            "temperature_warning_celsius": 80,
            "power_warning_percent": 95,
        }
        listed = await client.get("/api/v1/gpus", headers=headers)
        (row,) = [g for g in listed.json() if g["id"] == str(gpu.id)]
        assert row["alert_thresholds"]["power_warning_percent"] == 95
        response = await client.put(url, json={}, headers=headers)
        assert response.status_code == 200 and response.json()["alert_thresholds"] is None


class TestFaithfulnessAlert:
    async def _agent(self, session: AsyncSession, owner) -> Agent:
        agent, version = await _make_agent(session, tools=[], owner=owner)
        self._version, self._owner = version, owner
        return agent

    async def _scores(self, session: AsyncSession, agent: Agent, *values: float) -> None:
        start = dt.datetime.now(dt.UTC)
        for i, value in enumerate(values):
            run = await _make_run(session, agent, self._version, user=self._owner, permissions=[])
            session.add(
                AgentAnswerScore(
                    run_id=run.id,
                    agent_id=agent.id,
                    question="q",
                    answer="a",
                    status=AnswerScoreStatus.SCORED,
                    faithfulness=value,
                    scored_at=start + dt.timedelta(seconds=i),
                )
            )
        await session.flush()

    def _service(self, session: AsyncSession, settings: Settings, **agents) -> AnswerScoringService:
        tuned = settings.model_copy(update={"agents": settings.agents.model_copy(update=agents)})
        repo = AlertRepository(session)
        return AnswerScoringService(
            tuned,
            AgentAnswerScoreRepository(session),
            answers=None,  # type: ignore[arg-type]
            alerts=AlertService(repo, AuditService(AuditRepository(session))),
        )

    async def _open(self, session: AsyncSession, agent: Agent) -> Alert | None:
        return (
            await session.execute(
                select(Alert).where(
                    Alert.fingerprint == f"{FAITHFULNESS_ALERT_SOURCE}:{agent.id}",
                    Alert.state != AlertState.RESOLVED,
                )
            )
        ).scalar_one_or_none()

    async def test_raised_below_the_line_and_cleared_above(
        self, session: AsyncSession, settings: Settings, superuser
    ) -> None:
        await session.execute(delete(Alert).where(Alert.source == FAITHFULNESS_ALERT_SOURCE))
        agent = await self._agent(session, superuser)
        service = self._service(
            session,
            settings,
            answer_faithfulness_alert_below=0.7,
            answer_faithfulness_alert_answers=3,
        )
        await self._scores(session, agent, 0.5, 0.6)
        await service.check_faithfulness(agent, agent.id)
        assert await self._open(session, agent) is None  # only two answers so far

        await self._scores(session, agent, 0.6)
        await service.check_faithfulness(agent, agent.id)
        alert = await self._open(session, agent)
        assert alert.severity == AlertSeverity.WARNING and "57%" in alert.title

        await self._scores(session, agent, 1.0, 1.0, 1.0)
        await service.check_faithfulness(agent, agent.id)
        await session.refresh(alert)
        assert alert.state == AlertState.RESOLVED

    async def test_off_by_default(
        self, session: AsyncSession, settings: Settings, superuser
    ) -> None:
        agent = await self._agent(session, superuser)
        await self._scores(session, agent, *([0.1] * 25))
        await self._service(session, settings).check_faithfulness(agent, agent.id)
        assert await self._open(session, agent) is None

    async def test_a_stale_or_disabled_agent_has_its_alert_cleared(
        self, session: AsyncSession, settings: Settings, superuser
    ) -> None:
        """E87: nothing new would clear the alert of an agent nobody uses any more."""
        await session.execute(delete(Alert).where(Alert.source == FAITHFULNESS_ALERT_SOURCE))
        service = self._service(
            session,
            settings,
            answer_faithfulness_alert_below=0.7,
            answer_faithfulness_alert_answers=3,
            answer_faithfulness_stale_days=14,
        )
        fresh = await self._agent(session, superuser)
        await self._scores(session, fresh, 0.1, 0.1, 0.1)
        await service.check_faithfulness(fresh, fresh.id)
        stale = await self._agent(session, superuser)
        await self._scores(session, stale, 0.1, 0.1, 0.1)
        await service.check_faithfulness(stale, stale.id)
        disabled = await self._agent(session, superuser)
        await self._scores(session, disabled, 0.1, 0.1, 0.1)
        await service.check_faithfulness(disabled, disabled.id)
        assert all([await self._open(session, a) for a in (fresh, stale, disabled)])

        old = dt.datetime.now(dt.UTC) - dt.timedelta(days=20)
        for row in (
            await session.execute(
                select(AgentAnswerScore).where(AgentAnswerScore.agent_id == stale.id)
            )
        ).scalars():
            row.scored_at = old
        disabled.enabled = False
        await session.flush()

        assert await service.clear_stale_alerts() == 2
        assert await self._open(session, fresh) is not None
        assert await self._open(session, stale) is None
        assert await self._open(session, disabled) is None

    async def test_turning_the_line_off_clears_them(
        self, session: AsyncSession, settings: Settings, superuser
    ) -> None:
        await session.execute(delete(Alert).where(Alert.source == FAITHFULNESS_ALERT_SOURCE))
        agent = await self._agent(session, superuser)
        await self._scores(session, agent, 0.1, 0.1, 0.1)
        on = self._service(
            session,
            settings,
            answer_faithfulness_alert_below=0.7,
            answer_faithfulness_alert_answers=3,
        )
        await on.check_faithfulness(agent, agent.id)
        assert await self._open(session, agent) is not None
        assert await self._service(session, settings).clear_stale_alerts() == 1
        assert await self._open(session, agent) is None
