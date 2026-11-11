"""Chargeback: cost rates, cost reports and CSV export (E9).

Usage is recorded in January 2020, a period no other test writes to, and every model name
is unique per test, so assertions can be exact on a shared database.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import uuid
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.models.chargeback import CostRate
from app.models.infrastructure import GpuAllocation, Node, NodeStatus
from app.models.models_registry import Model, ModelStatus, UsageRecord
from app.models.teams import Team, TeamMembership
from app.services.chargeback import RateSchedule, period_bounds
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/chargeback"
UTC = dt.UTC
PERIOD = {"start": "2020-01-01", "end": "2020-01-31"}


def _at(day: int, hour: int = 12) -> dt.datetime:
    return dt.datetime(2020, 1, day, hour, tzinfo=UTC)


def _name(prefix: str = "cb") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _usage(
    session: AsyncSession,
    model: str,
    *,
    when: dt.datetime,
    prompt: int,
    completion: int,
    team: Team | None = None,
    end_user: str | None = None,
    trusted: bool = False,
    disconnected: bool = False,
) -> None:
    session.add(
        UsageRecord(
            recorded_at=when,
            endpoint="chat.completions",
            requested_model=model,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            team_id=team.id if team else None,
            end_user=end_user,
            end_user_trusted=trusted,
            client_disconnected=disconnected,
        )
    )
    await session.flush()


async def _rate(
    session: AsyncSession,
    model: str,
    *,
    since: dt.datetime,
    prompt: str,
    completion: str,
    gpu_hour: str | None = None,
) -> CostRate:
    rate = CostRate(
        model=model,
        effective_from=since,
        prompt_per_million=Decimal(prompt),
        completion_per_million=Decimal(completion),
        gpu_hour=None if gpu_hour is None else Decimal(gpu_hour),
    )
    session.add(rate)
    await session.flush()
    return rate


async def _team(session: AsyncSession) -> Team:
    suffix = uuid.uuid4().hex[:8]
    team = Team(name=f"Cost {suffix}", slug=f"cost-{suffix}")
    session.add(team)
    await session.flush()
    return team


async def _gpu_model(session: AsyncSession, name: str, *, hours: int) -> None:
    """A registered model whose deployment held one GPU for ``hours`` from 10 Jan."""
    model = Model(
        name=name,
        display_name=name,
        type="LLM",
        storage_path="/data/models/cb",
        runtime="mock",
        context_length=4096,
        status=ModelStatus.AVAILABLE,
    )
    node = Node(
        name=_name("node"),
        agent_url="http://agent:9100",
        agent_token_encrypted="x",
        status=NodeStatus.ONLINE,
    )
    session.add_all([model, node])
    await session.flush()
    start = _at(10, 0)
    session.add(
        GpuAllocation(
            node_id=node.id,
            gpu_index=0,
            reservation_id=uuid.uuid4(),
            purpose=f"model:{model.id}",
            reserved_at=start,
            released_at=start + dt.timedelta(hours=hours),
        )
    )
    await session.flush()


async def _report(client: AsyncClient, tokens, user: User, **params: str) -> dict[str, Any]:
    response = await client.get(
        f"{URL}/report", params={**PERIOD, **params}, headers=auth_header(tokens, user)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _row(body: dict[str, Any], key: str | None) -> dict[str, Any]:
    return next(r for r in body["rows"] if r["key"] == key)


class TestRateSchedule:
    def _rate(self, model: str, day: int) -> CostRate:
        return CostRate(
            model=model,
            effective_from=_at(day, 0),
            prompt_per_million=Decimal(day),
            completion_per_million=Decimal(0),
        )

    def test_own_rate_beats_a_newer_default(self) -> None:
        own, default = self._rate("m", 1), self._rate("*", 5)
        schedule = RateSchedule([own, default])
        assert schedule.at("m", _at(10)) is own
        assert schedule.at("other", _at(10)) is default
        assert schedule.at("other", _at(2)) is None

    def test_segments_cut_at_each_rate_change(self) -> None:
        first, second = self._rate("m", 1), self._rate("m", 15)
        segments = RateSchedule([first, second]).segments("m", _at(10, 0), _at(20, 0))
        assert [(a.day, b.day, r) for a, b, r in segments] == [(10, 15, first), (15, 20, second)]

    def test_period_is_whole_utc_days_and_bounded(self) -> None:
        begin, finish = period_bounds(dt.date(2020, 1, 1), dt.date(2020, 1, 31))
        assert begin == dt.datetime(2020, 1, 1, tzinfo=UTC)
        assert finish == dt.datetime(2020, 2, 1, tzinfo=UTC)


class TestRates:
    async def test_create_list_and_delete_are_audited(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        model = _name()
        await _usage(session, model, when=_at(2), prompt=1, completion=1)
        headers = auth_header(tokens, superuser)
        created = await client.post(
            f"{URL}/rates",
            json={
                "model": model,
                "prompt_per_million": "0.50",
                "completion_per_million": "1.5",
                "gpu_hour": "2",
                "effective_from": "2019-12-01T00:00:00Z",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["status"] == "in_force"
        assert Decimal(body["prompt_per_million"]) == Decimal("0.5")
        assert body["currency"] == "USD"

        listed = (await client.get(f"{URL}/rates", headers=headers)).json()
        assert any(r["id"] == body["id"] for r in listed["items"])

        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.COST_RATE_CREATED,
                    AuditLog.resource_id == body["id"],
                )
            )
        ).scalar_one()
        assert audit.meta["backdated"] is True

        deleted = await client.request(
            "DELETE", f"{URL}/rates/{body['id']}", json={"reason": "typo"}, headers=headers
        )
        assert deleted.status_code == 204
        assert await session.get(CostRate, uuid.UUID(body["id"])) is None
        removed = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.COST_RATE_DELETED,
                    AuditLog.resource_id == body["id"],
                )
            )
        ).scalar_one()
        assert removed.meta["reason"] == "typo"

    async def test_unknown_model_duplicate_and_negative_are_refused(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        payload = {
            "model": "no-such-model-anywhere",
            "prompt_per_million": "1",
            "completion_per_million": "1",
        }
        assert (await client.post(f"{URL}/rates", json=payload, headers=headers)).status_code == 422

        negative = {**payload, "model": "*", "prompt_per_million": "-1"}
        assert (
            await client.post(f"{URL}/rates", json=negative, headers=headers)
        ).status_code == 422

        model = _name()
        await _usage(session, model, when=_at(2), prompt=1, completion=1)
        once = {**payload, "model": model, "effective_from": "2019-06-01T00:00:00Z"}
        assert (await client.post(f"{URL}/rates", json=once, headers=headers)).status_code == 201
        assert (await client.post(f"{URL}/rates", json=once, headers=headers)).status_code == 409

    async def test_permissions(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        nobody = await _user_with(session, settings, [], name="nocost")
        viewer = await _user_with(session, settings, [Perm.COST_VIEW], name="costview")
        assert (
            await client.get(f"{URL}/rates", headers=auth_header(tokens, nobody))
        ).status_code == 403
        assert (
            await client.get(f"{URL}/report", headers=auth_header(tokens, nobody))
        ).status_code == 403
        assert (
            await client.get(f"{URL}/rates", headers=auth_header(tokens, viewer))
        ).status_code == 200
        create = await client.post(
            f"{URL}/rates",
            json={"model": "*", "prompt_per_million": "1", "completion_per_million": "1"},
            headers=auth_header(tokens, viewer),
        )
        assert create.status_code == 403


class TestReport:
    async def test_tokens_priced_at_the_rate_in_force_when_called(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        model = _name()
        await _rate(session, model, since=_at(1, 0), prompt="1", completion="2")
        await _rate(session, model, since=_at(15, 0), prompt="10", completion="20")
        await _usage(session, model, when=_at(5), prompt=1_000_000, completion=500_000)
        await _usage(session, model, when=_at(20), prompt=100_000, completion=0, disconnected=True)

        body = await _report(client, tokens, superuser, group_by="model")
        row = _row(body, model)
        # 1.0 + 1.0 before the change; 1.0 after it.
        assert Decimal(row["token_cost"]) == Decimal("3.0000")
        assert row["requests"] == 2
        assert row["partial_requests"] == 1
        assert row["unpriced_requests"] == 0
        assert body["estimate"] is True
        assert body["currency"] == "USD"

    async def test_usage_without_a_rate_is_unpriced_not_free(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        model = _name()
        await _usage(session, model, when=_at(3), prompt=1000, completion=1000)
        body = await _report(client, tokens, superuser, group_by="model")
        row = _row(body, model)
        assert row["token_cost"] is None
        assert row["total_cost"] is None
        assert row["unpriced_requests"] == 1
        assert model in body["unpriced_models"]

    async def test_default_rate_applies_to_models_without_their_own(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        own, other = _name(), _name()
        await _rate(session, "*", since=_at(1, 0), prompt="100", completion="100")
        await _rate(session, own, since=_at(1, 0), prompt="1", completion="1")
        await _usage(session, own, when=_at(4), prompt=1_000_000, completion=0)
        await _usage(session, other, when=_at(4), prompt=1_000_000, completion=0)
        body = await _report(client, tokens, superuser, group_by="model")
        assert Decimal(_row(body, own)["token_cost"]) == Decimal(1)
        assert Decimal(_row(body, other)["token_cost"]) == Decimal(100)

    async def test_gpu_hours_shared_by_token_share_and_idle_reported(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        busy, idle = _name("busy"), _name("idle")
        await _gpu_model(session, busy, hours=10)
        await _gpu_model(session, idle, hours=4)
        await _rate(session, busy, since=_at(1, 0), prompt="0", completion="0", gpu_hour="3")
        await _rate(session, idle, since=_at(1, 0), prompt="0", completion="0", gpu_hour="5")
        first, second = await _team(session), await _team(session)
        await _usage(session, busy, when=_at(10, 5), prompt=300, completion=0, team=first)
        await _usage(session, busy, when=_at(10, 6), prompt=700, completion=0, team=second)

        body = await _report(client, tokens, superuser, group_by="team")
        a, b = _row(body, str(first.id)), _row(body, str(second.id))
        assert a["gpu_hours"] == 3.0 and Decimal(a["gpu_cost"]) == Decimal(9)
        assert b["gpu_hours"] == 7.0 and Decimal(b["gpu_cost"]) == Decimal(21)
        assert Decimal(a["total_cost"]) == Decimal(9)  # tokens priced at zero, not null
        assert body["idle"]["gpu_hours"] == 4.0
        assert Decimal(body["idle"]["gpu_cost"]) == Decimal(20)
        assert idle in body["idle"]["detail"]

    async def test_scoped_caller_sees_own_team_and_a_fair_gpu_share(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        model = _name("shared")
        await _gpu_model(session, model, hours=10)
        await _rate(session, model, since=_at(1, 0), prompt="0", completion="0", gpu_hour="1")
        mine, theirs = await _team(session), await _team(session)
        await _usage(session, model, when=_at(10, 1), prompt=100, completion=0, team=mine)
        await _usage(session, model, when=_at(10, 2), prompt=900, completion=0, team=theirs)
        member = await _user_with(session, settings, [Perm.COST_VIEW], name="costmember")
        session.add(TeamMembership(team_id=mine.id, user_id=member.id))
        await session.flush()

        body = await _report(client, tokens, member, group_by="team")
        keys = {r["key"] for r in body["rows"]}
        assert str(mine.id) in keys and str(theirs.id) not in keys
        # 10 % of the tokens, so 10 % of the GPU time, not all of it.
        assert _row(body, str(mine.id))["gpu_hours"] == 1.0
        assert body["idle"] is None

    async def test_end_users_split_by_trust(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        model, person = _name(), _name("person")
        await _usage(
            session, model, when=_at(6), prompt=1, completion=1, end_user=person, trusted=True
        )
        await _usage(session, model, when=_at(6), prompt=1, completion=1, end_user=person)
        body = await _report(client, tokens, superuser, group_by="end_user")
        mine = [r for r in body["rows"] if r["key"] == person]
        assert sorted(r["detail"] for r in mine) == ["self-reported", "trusted"]

    async def test_period_validation(self, client: AsyncClient, tokens, superuser: User) -> None:
        headers = auth_header(tokens, superuser)
        reversed_ = await client.get(
            f"{URL}/report", params={"start": "2020-02-01", "end": "2020-01-01"}, headers=headers
        )
        assert reversed_.status_code == 422
        too_long = await client.get(
            f"{URL}/report", params={"start": "2018-01-01", "end": "2020-01-01"}, headers=headers
        )
        assert too_long.status_code == 422
        bad_group = await client.get(
            f"{URL}/report", params={**PERIOD, "group_by": "planet"}, headers=headers
        )
        assert bad_group.status_code == 422


class TestExport:
    async def test_csv_is_audited_and_formula_safe(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        model = _name()
        await _rate(session, model, since=_at(1, 0), prompt="1", completion="1")
        await _usage(
            session, model, when=_at(7), prompt=1_000_000, completion=0, end_user="=HYPERLINK(1)"
        )
        response = await client.post(
            f"{URL}/export",
            params={**PERIOD, "group_by": "end_user"},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(response.text)))
        planted = next(r for r in rows if r["key"].endswith("HYPERLINK(1)"))
        assert planted["key"] == "'=HYPERLINK(1)"
        assert planted["label"] == "'=HYPERLINK(1)"
        assert planted["estimate"] == "true"
        assert all(not r["label"].startswith("=") for r in rows)

        audit = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.action == AuditAction.CHARGEBACK_EXPORTED)
                .order_by(AuditLog.timestamp.desc())
                .limit(1)
            )
        ).scalar_one()
        assert audit.meta["group_by"] == "end_user"
