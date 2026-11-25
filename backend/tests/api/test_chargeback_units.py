"""E67: what isn't counted in tokens is priced by its unit.

Transcription per audio minute, speech per million characters, agent tool calls per call.
January 2020 and unique model and tool names, as in ``test_chargeback.py``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.interfaces.media import OcrBlock, OcrResult
from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.models.agents import ApprovalState, ToolExecution
from app.models.auth import User
from app.models.chargeback import CostRate
from app.models.models_registry import UsageRecord
from app.models.teams import TeamMembership
from app.models.workflows import Workflow, WorkflowRun, WorkflowStep
from app.repositories.audit import AuditRepository
from app.repositories.chargeback import ChargebackRepository, CostRateRepository
from app.services.audit import AuditService
from app.services.chargeback import ChargebackService
from app.services.media_provider import OCR_CHARGE, MeteredOcrEngine, OcrCharge
from tests.api.conftest import _user_with
from tests.api.test_agents import _make_agent, _make_run, _make_tool
from tests.api.test_chargeback import PERIOD, URL, _at, _name, _team
from tests.conftest import auth_header


async def _speech(session: AsyncSession, model: str, endpoint: str, units: int) -> None:
    session.add(
        UsageRecord(
            recorded_at=_at(5),
            endpoint=endpoint,
            requested_model=model,
            model=model,
            prompt_tokens=units,
            completion_tokens=0,
        )
    )
    await session.flush()


async def _rate(session: AsyncSession, model: str, *, unit: str | None, prompt: str = "0") -> None:
    session.add(
        CostRate(
            model=model,
            effective_from=_at(1, 0),
            prompt_per_million=Decimal(prompt),
            completion_per_million=Decimal(0),
            unit_price=None if unit is None else Decimal(unit),
        )
    )
    await session.flush()


async def _report(client: AsyncClient, tokens, user: User, group: str = "model") -> dict:
    r = await client.get(
        f"{URL}/report", params={**PERIOD, "group_by": group}, headers=auth_header(tokens, user)
    )
    assert r.status_code == 200, r.text
    return r.json()


def _row(report: dict, key: str | None) -> dict:
    return next(row for row in report["rows"] if row["key"] == key)


class TestSpeech:
    async def test_transcription_is_priced_per_audio_minute(
        self, client, tokens, superuser, session
    ):
        model = _name("asr")
        await _speech(session, model, "audio.transcriptions", 90)
        await _rate(session, model, unit="0.6", prompt="1000")  # token price ignored
        row = _row(await _report(client, tokens, superuser), model)
        assert row["audio_seconds"] == 90 and row["prompt_tokens"] == 0
        assert Decimal(row["speech_cost"]) == Decimal("0.9")
        assert row["token_cost"] is None
        assert Decimal(row["total_cost"]) == Decimal("0.9")

    async def test_speech_is_priced_per_million_characters(
        self, client, tokens, superuser, session
    ):
        model = _name("tts")
        await _speech(session, model, "audio.speech", 500_000)
        await _rate(session, model, unit="30")
        row = _row(await _report(client, tokens, superuser), model)
        assert row["speech_characters"] == 500_000
        assert Decimal(row["speech_cost"]) == Decimal("15")

    async def test_never_at_token_prices(self, client, tokens, superuser, session):
        model = _name("tts")
        await _speech(session, model, "audio.speech", 1_000_000)
        await _rate(session, model, unit=None, prompt="5")
        report = await _report(client, tokens, superuser)
        row = _row(report, model)
        assert row["total_cost"] is None and row["unpriced_requests"] == 1
        assert model in report["unpriced_models"]


class TestTools:
    async def _calls(self, session, owner: User, team, outcomes: list[bool | None]):
        tool = await _make_tool(session)
        agent, version = await _make_agent(session, tools=[tool], owner=owner)
        agent.team_id = team.id
        run = await _make_run(session, agent, version, user=owner, permissions=[])
        for success in outcomes:
            session.add(
                ToolExecution(
                    run_id=run.id,
                    tool_id=tool.id,
                    tool_name=tool.name,
                    arguments={},
                    risk_level="LOW",
                    approval_state=ApprovalState.NOT_REQUIRED
                    if success is not None
                    else ApprovalState.PENDING,
                    requested_at=_at(7),
                    success=success,
                )
            )
        await session.flush()
        return tool

    async def test_calls_that_ran_are_priced_per_call(self, client, tokens, superuser, session):
        team = await _team(session)
        # Two ran (one failed, still a call), one is waiting for approval.
        tool = await self._calls(session, superuser, team, [True, False, None])
        await _rate(session, f"tool:{tool.name}", unit="0.05")
        await _rate(session, "*", unit=None, prompt="99")  # the token default never applies

        row = _row(await _report(client, tokens, superuser), f"tool:{tool.name}")
        assert row["tool_calls"] == 2 and row["requests"] == 0
        assert Decimal(row["tool_cost"]) == Decimal("0.1")
        assert Decimal(row["total_cost"]) == Decimal("0.1")

        by_team = _row(await _report(client, tokens, superuser, "team"), str(team.id))
        assert by_team["tool_calls"] == 2 and Decimal(by_team["tool_cost"]) == Decimal("0.1")

    async def test_the_tool_default_and_unpriced_tools(self, client, tokens, superuser, session):
        team = await _team(session)
        tool = await self._calls(session, superuser, team, [True])
        report = await _report(client, tokens, superuser)
        row = _row(report, f"tool:{tool.name}")
        assert row["tool_cost"] is None and row["unpriced_tool_calls"] == 1
        # Unpriced tools don't count as unpriced model usage.
        assert row["unpriced_requests"] == 0

        await _rate(session, "tool:*", unit="0.2")
        row = _row(await _report(client, tokens, superuser), f"tool:{tool.name}")
        assert Decimal(row["tool_cost"]) == Decimal("0.2") and row["unpriced_tool_calls"] == 0

    async def test_scoped_reports_see_their_teams_tools_only(
        self, client, tokens, superuser, session, settings
    ):
        mine, theirs = await _team(session), await _team(session)
        await self._calls(session, superuser, mine, [True])
        await self._calls(session, superuser, theirs, [True, True])
        member = await _user_with(session, settings, [Perm.COST_VIEW], name="toolcost")
        session.add(TeamMembership(team_id=mine.id, user_id=member.id))
        await session.flush()

        report = await _report(client, tokens, member, "team")
        keys = {row["key"] for row in report["rows"]}
        assert str(mine.id) in keys and str(theirs.id) not in keys
        assert _row(report, str(mine.id))["tool_calls"] == 1


class TestRates:
    async def test_tool_rates_are_checked(self, client, tokens, superuser, session):
        tool = await _make_tool(session)
        headers = auth_header(tokens, superuser)
        post = lambda body: client.post(f"{URL}/rates", json=body, headers=headers)  # noqa: E731

        r = await post({"model": "tool:no-such-tool", "unit_price": "0.1"})
        assert r.status_code == 422 and "No tool named" in r.text
        r = await post({"model": f"tool:{tool.name}"})
        assert r.status_code == 422 and "unit_price" in r.text
        r = await post(
            {"model": f"tool:{tool.name}", "unit_price": "0.1", "prompt_per_million": "1"}
        )
        assert r.status_code == 422 and "calls only" in r.text

        r = await post({"model": f"tool:{tool.name}", "unit_price": "0.1"})
        assert r.status_code == 201, r.text
        r = await post({"model": "tool:*", "unit_price": "0.01"})
        assert r.status_code == 201, r.text
        listed = (await client.get(f"{URL}/rates", headers=headers)).json()["items"]
        mine = next(i for i in listed if i["model"] == f"tool:{tool.name}")
        assert Decimal(mine["unit_price"]) == Decimal("0.1")

    async def test_a_model_rate_takes_a_unit_price(self, client, tokens, superuser, session):
        model = _name("asr")
        await _speech(session, model, "audio.transcriptions", 60)
        r = await client.post(
            f"{URL}/rates",
            json={"model": model, "unit_price": "0.006", "effective_from": "2020-01-01T00:00:00Z"},
            headers=auth_header(tokens, superuser),
        )
        assert r.status_code == 201, r.text
        assert Decimal(r.json()["unit_price"]) == Decimal("0.006")


# ---------------------------------------------------------------------------
# E86: OCR per page, workflow tool steps, another currency
# ---------------------------------------------------------------------------
class TestOcr:
    async def test_pages_are_priced_per_page(self, client, tokens, superuser, session):
        model = _name("ocr")
        await _speech(session, model, "ocr", 7)
        await _rate(session, model, unit="0.01", prompt="1000")  # token price ignored
        row = _row(await _report(client, tokens, superuser), model)
        assert row["ocr_pages"] == 7 and row["prompt_tokens"] == 0
        assert Decimal(row["ocr_cost"]) == Decimal("0.07")
        assert Decimal(row["total_cost"]) == Decimal("0.07")

    async def test_a_metered_engine_records_pages(self) -> None:
        recorded: list[tuple] = []

        class Engine:
            async def recognise(self, image, **_):
                return OcrResult(blocks=(OcrBlock("a", page=1), OcrBlock("b", page=3)))

            async def health(self):
                return True

        async def meter(pages, status, latency, charge):
            recorded.append((pages, status, charge))

        engine = MeteredOcrEngine(Engine(), meter)
        charge = OcrCharge(team_id=uuid.uuid4())
        token = OCR_CHARGE.set(charge)
        try:
            await engine.recognise(b"x", model="m")
        finally:
            OCR_CHARGE.reset(token)
        await engine.recognise(b"x", model="m")
        assert recorded == [(2, 200, charge), (2, 200, None)]


class TestWorkflowTools:
    async def test_tool_steps_that_ran_are_priced(self, client, tokens, superuser, session):
        team = await _team(session)
        tool = await _make_tool(session)
        workflow = Workflow(name=_name("wf"), display_name="Priced", team_id=team.id)
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(
            workflow_id=workflow.id,
            version=1,
            definition={"nodes": []},
            trigger="manual",
            status="SUCCEEDED",
            started_by=superuser.id,
        )
        session.add(run)
        await session.flush()
        for sequence, (status, output) in enumerate(
            [
                ("SUCCEEDED", {"content": "ok", "data": None, "truncated": False}),
                ("SUCCEEDED", {"awaiting": "approval", "tool": tool.name}),  # only asked
                ("FAILED", None),  # never ran the tool
            ],
            start=1,
        ):
            session.add(
                WorkflowStep(
                    run_id=run.id,
                    sequence=sequence,
                    node_id=f"call{sequence}",
                    node_type="tool",
                    status=status,
                    input={"tool_id": str(tool.id), "arguments": {}},
                    output=output,
                    started_at=_at(8),
                )
            )
        await session.flush()
        await _rate(session, f"tool:{tool.name}", unit="0.25")

        row = _row(await _report(client, tokens, superuser), f"tool:{tool.name}")
        assert row["tool_calls"] == 1 and Decimal(row["tool_cost"]) == Decimal("0.25")
        by_team = _row(await _report(client, tokens, superuser, "team"), str(team.id))
        assert by_team["tool_calls"] == 1


class TestCurrency:
    async def test_shown_in_another_currency(self, session, superuser) -> None:
        model = _name("fx")
        await _speech(session, model, "ocr", 10)
        await _rate(session, model, unit="1")
        audit = AuditService(AuditRepository(session))
        service = ChargebackService(
            CostRateRepository(session),
            ChargebackRepository(session),
            audit,
            currency="USD",
            exchange_rates={"EUR": Decimal("0.5")},
        )
        start, end = dt.date(2020, 1, 1), dt.date(2020, 1, 31)
        report = await service.report(
            group="model", start=start, end=end, scope=Scope.everything(), currency="EUR"
        )
        row = _row(report, model)
        assert report["currency"] == "EUR" and report["priced_in"] == "USD"
        assert report["exchange_rate"] == Decimal("0.5")
        assert report["currencies"] == ["EUR", "USD"]
        assert row["ocr_cost"] == Decimal("5") and row["total_cost"] == Decimal("5")
        plain = await service.report(group="model", start=start, end=end, scope=Scope.everything())
        assert _row(plain, model)["total_cost"] == Decimal("10")

    async def test_unknown_currency_is_refused(self, client, tokens, superuser) -> None:
        r = await client.get(
            f"{URL}/report",
            params={**PERIOD, "currency": "JPY"},
            headers=auth_header(tokens, superuser),
        )
        assert r.status_code == 422 and "No exchange rate for JPY" in r.text
