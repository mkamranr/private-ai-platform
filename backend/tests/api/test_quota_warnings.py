"""Quota warnings, extended (E53): a warning share per quota, ``X-Quota-Warning`` on gateway
responses, and a near-limit status on the quotas list.

Usage is planted as committed usage records, which is where budget counters are seeded
from, so each test starts from a known share of its budget.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.models_registry import Model, UsageRecord
from app.models.operations import AlertSeverity, GatewayQuota
from app.services.budgets import level
from app.services.gateway_policy import QUOTA_WARNING_HEADER, Admission, QuotaWarning
from app.services.quotas import quota_status
from tests.api.conftest import _user_with
from tests.conftest import auth_header

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def quota_admin(session: AsyncSession, settings) -> User:
    return await _user_with(
        session, settings, [Perm.QUOTA_VIEW, Perm.QUOTA_MANAGE, Perm.USAGE_VIEW], name="quota"
    )


async def _plant_usage(database, key_id: uuid.UUID, model: str, tokens: int) -> None:
    async with database.sessionmaker() as other:
        other.add(
            UsageRecord(
                api_key_id=key_id,
                endpoint="chat.completions",
                requested_model=model,
                model=model,
                prompt_tokens=tokens,
            )
        )
        await other.commit()


async def _chat(client: AsyncClient, secret: str, model: str, **extra: Any) -> Response:
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}"},
        json={"model": model, "messages": MESSAGES, **extra},
    )


class TestHeader:
    async def test_past_the_warning_share_the_response_says_so(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        database,
        committed_gateway: dict[str, Any],
    ) -> None:
        key_id, model = committed_gateway["key_id"], committed_gateway["model_name"]
        await _plant_usage(database, key_id, model, 850)
        session.add(GatewayQuota(scope="api_key", subject=str(key_id), monthly_tokens=1000))
        await session.flush()

        response = await _chat(client, committed_gateway["secret"], model)
        assert response.status_code == 200, response.text
        assert response.headers[QUOTA_WARNING_HEADER] == "api_key; period=monthly; used=85%"

    async def test_streamed_responses_carry_it_too(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        database,
        committed_gateway: dict[str, Any],
    ) -> None:
        key_id, model = committed_gateway["key_id"], committed_gateway["model_name"]
        await _plant_usage(database, key_id, model, 900)
        session.add(GatewayQuota(scope="api_key", subject=str(key_id), daily_tokens=1000))
        await session.flush()

        response = await _chat(client, committed_gateway["secret"], model, stream=True)
        assert response.status_code == 200, response.text
        assert response.headers[QUOTA_WARNING_HEADER] == "api_key; period=daily; used=90%"

    async def test_below_the_share_there_is_no_header(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        database,
        committed_gateway: dict[str, Any],
    ) -> None:
        key_id, model = committed_gateway["key_id"], committed_gateway["model_name"]
        await _plant_usage(database, key_id, model, 850)
        session.add(
            GatewayQuota(scope="api_key", subject=str(key_id), monthly_tokens=1000, warn_percent=90)
        )
        await session.flush()

        response = await _chat(client, committed_gateway["secret"], model)
        assert response.status_code == 200
        assert QUOTA_WARNING_HEADER not in response.headers

    def test_several_warnings_most_used_first(self) -> None:
        admission = Admission(
            warnings=[QuotaWarning("team", "monthly", 81), QuotaWarning("api_key", "daily", 95)]
        )
        assert admission.warning_header() == (
            "api_key; period=daily; used=95%, team; period=monthly; used=81%"
        )
        assert Admission().warning_header() is None


class TestStatusAndAlerts:
    def test_status(self) -> None:
        quota = GatewayQuota(scope="model", subject="m", daily_tokens=100, monthly_tokens=1000)
        assert quota_status(quota, {"day": 10, "month": 100}, 80) == ("ok", 10)
        assert quota_status(quota, {"day": 85, "month": 100}, 80) == ("near_limit", 85)
        assert quota_status(quota, {"day": 85, "month": 1000}, 80) == ("exhausted", 100)
        wildcard = GatewayQuota(scope="model", subject="*", daily_tokens=100)
        assert quota_status(wildcard, {"day": None}, 80) == ("unknown", None)
        rate_only = GatewayQuota(scope="model", subject="m", tokens_per_minute=100)
        assert quota_status(rate_only, {"minute": 99}, 80) == ("ok", None)

    def test_the_alert_level_uses_the_quota_share(self) -> None:
        # The budget watcher passes `quota.warn_percent or <global>` to `level`.
        assert level(Decimal(85), Decimal(100), 80) == AlertSeverity.WARNING
        assert level(Decimal(85), Decimal(100), 90) is None


class TestApi:
    async def test_warn_percent_is_set_shown_and_validated(
        self,
        client: AsyncClient,
        tokens,
        quota_admin: User,
        registered_model: Model,
    ) -> None:
        headers = auth_header(tokens, quota_admin)
        created = await client.post(
            "/api/v1/quotas",
            json={
                "scope": "model",
                "subject": registered_model.name,
                "daily_tokens": 1000,
                "warn_percent": 60,
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["warn_percent"] == 60 and body["effective_warn_percent"] == 60
        assert body["status"] == "ok"

        quota_id = body["id"]
        cleared = await client.patch(
            f"/api/v1/quotas/{quota_id}", json={"warn_percent": None}, headers=headers
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["warn_percent"] is None
        assert cleared.json()["effective_warn_percent"] == 80  # the platform default

        refused = await client.patch(
            f"/api/v1/quotas/{quota_id}", json={"warn_percent": 100}, headers=headers
        )
        assert refused.status_code == 422
