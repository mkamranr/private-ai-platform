"""Settings page: runtime overrides and read-only deployment settings (E8)."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from app.models.system import SystemSetting
from app.services.runtime_settings import (
    BY_KEY,
    DEPLOYMENT_SETTINGS,
    RUNTIME,
    RUNTIME_SETTINGS,
    apply_overrides,
)
from tests.api.conftest import _user_with
from tests.conftest import auth_header

URL = "/api/v1/settings"


def _item(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(i for i in body["items"] if i["key"] == key)


async def _audits(session: AsyncSession, action: AuditAction, key: str) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(AuditLog.action == action, AuditLog.resource_id == key)
    )
    return list(rows.scalars())


class TestCatalogue:
    async def test_lists_runtime_and_deployment_settings(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        body = (await client.get(URL, headers=auth_header(tokens, superuser))).json()
        keys = {i["key"] for i in body["items"]}
        assert keys == {s.key for s in RUNTIME_SETTINGS} | {s.key for s in DEPLOYMENT_SETTINGS}
        assert {c["id"] for c in body["categories"]} >= {i["category"] for i in body["items"]}
        runtime = _item(body, "gateway.circuit_failure_threshold")
        assert runtime["editable"] is True
        assert runtime["env_var"] == "GATEWAY__CIRCUIT_FAILURE_THRESHOLD"
        assert runtime["source"] in {"default", "configured"}
        deployment = _item(body, "auth.access_token_ttl_seconds")
        assert deployment["editable"] is False
        assert deployment["applies"] == "Restart"
        # A lifetime is not a secret, even though its name contains "token".
        assert isinstance(deployment["value"], int)

    async def test_secrets_are_never_shown(
        self, client: AsyncClient, tokens, superuser: User, settings
    ) -> None:
        response = await client.get(URL, headers=auth_header(tokens, superuser))
        assert _item(response.json(), "auth.jwt_secret_key")["value"] == "[REDACTED]"
        assert _item(response.json(), "security.encryption_key")["value"] == "[REDACTED]"
        for secret in (
            settings.auth.jwt_secret_key.get_secret_value(),
            settings.security.encryption_key.get_secret_value(),
        ):
            assert secret not in response.text

    async def test_view_needs_settings_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        nobody = await _user_with(session, settings, [], name="nobody")
        assert (await client.get(URL, headers=auth_header(tokens, nobody))).status_code == 403


class TestOverrides:
    async def test_override_is_validated_stored_audited_and_applied(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        response = await client.put(
            f"{URL}/ops.runner_stale_seconds",
            json={"value": 300, "reason": "runner host is slow to restart"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        item = _item(response.json(), "ops.runner_stale_seconds")
        assert item["source"] == "override"
        assert item["value"] == 300
        assert item["configured_value"] != 300

        row = await session.get(SystemSetting, "runtime.ops.runner_stale_seconds")
        assert row is not None and row.value == 300
        [audit] = await _audits(session, AuditAction.SETTING_UPDATED, "ops.runner_stale_seconds")
        assert audit.meta["reason"] == "runner host is slow to restart"

        # The consumer sees it on the next request, with no restart.
        status = (await client.get("/api/v1/ops/runner", headers=headers)).json()
        assert status["stale_after_seconds"] == 300

    async def test_reset_returns_to_the_deployment_value(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession, settings
    ) -> None:
        headers = auth_header(tokens, superuser)
        await client.put(f"{URL}/ops.runner_stale_seconds", json={"value": 300}, headers=headers)
        response = await client.delete(f"{URL}/ops.runner_stale_seconds", headers=headers)
        assert response.status_code == 200
        item = _item(response.json(), "ops.runner_stale_seconds")
        assert item["source"] != "override"
        assert item["value"] == settings.ops.runner_stale_seconds
        assert await session.get(SystemSetting, "runtime.ops.runner_stale_seconds") is None
        assert await _audits(session, AuditAction.SETTING_RESET, "ops.runner_stale_seconds")
        status = (await client.get("/api/v1/ops/runner", headers=headers)).json()
        assert status["stale_after_seconds"] == settings.ops.runner_stale_seconds

    async def test_values_get_the_environments_validation(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        headers = auth_header(tokens, superuser)
        for key, value in [
            ("gateway.circuit_failure_threshold", 0),  # Field(ge=1)
            ("gateway.circuit_failure_threshold", "lots"),
            ("ops.runner_stale_seconds", 5),  # Field(ge=30)
            ("gpu.metric_retention_days", 0),  # the definition's own bound
            ("platform.public_base_url", "ftp://files.local"),
            ("platform.public_base_url", "https://user:pw@ai.local"),
        ]:
            response = await client.put(f"{URL}/{key}", json={"value": value}, headers=headers)
            assert response.status_code == 422, (key, value, response.text)

    async def test_deployment_settings_cannot_be_overridden(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        headers = auth_header(tokens, superuser)
        for key in ("auth.jwt_secret_key", "agents.disabled_tool_types", "airgap.enforced"):
            response = await client.put(f"{URL}/{key}", json={"value": "x"}, headers=headers)
            assert response.status_code == 404, key

    async def test_changing_needs_settings_manage(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.SETTINGS_VIEW], name="viewer")
        headers = auth_header(tokens, viewer)
        assert (await client.get(URL, headers=headers)).status_code == 200
        put = await client.put(
            f"{URL}/ops.runner_stale_seconds", json={"value": 300}, headers=headers
        )
        assert put.status_code == 403
        delete = await client.delete(f"{URL}/ops.runner_stale_seconds", headers=headers)
        assert delete.status_code == 403

    async def test_an_invalid_stored_value_is_ignored_and_reported(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession, settings
    ) -> None:
        """An upgrade can tighten a bound under an existing override. The override is then
        ignored, not applied, and the page says why."""
        session.add(
            SystemSetting(
                key="runtime.gateway.circuit_failure_threshold", value=0, category="gateway"
            )
        )
        await session.flush()
        body = (await client.get(URL, headers=auth_header(tokens, superuser))).json()
        item = _item(body, "gateway.circuit_failure_threshold")
        assert item["source"] != "override"
        assert item["problem"]
        effective = await RUNTIME.effective(session, settings)
        assert (
            effective.gateway.circuit_failure_threshold
            == settings.gateway.circuit_failure_threshold
        )


class TestApplying:
    def test_overrides_are_coerced_and_scoped_to_their_field(self, settings) -> None:
        base_retention = settings.gpu.metric_retention_days
        effective, problems = apply_overrides(
            settings,
            {"gateway.circuit_open_seconds": "45", "gpu.metric_retention_days": 9},
        )
        assert problems == {}
        assert effective.gateway.circuit_open_seconds == 45.0
        assert effective.gpu.metric_retention_days == 9
        # Everything else is the deployment's, and the base object is untouched.
        assert effective.gateway.circuit_window_seconds == settings.gateway.circuit_window_seconds
        assert settings.gpu.metric_retention_days == base_retention
        assert effective is not settings

    def test_unknown_keys_are_reported_not_applied(self, settings) -> None:
        effective, problems = apply_overrides(settings, {"auth.jwt_algorithm": "none"})
        assert effective is settings
        assert "auth.jwt_algorithm" in problems

    async def test_cache_reloads_after_invalidation(self, session: AsyncSession, settings) -> None:
        before = await RUNTIME.effective(session, settings)
        session.add(SystemSetting(key="runtime.ops.max_log_lines", value=150, category="ops"))
        await session.flush()
        # Still cached: other processes see a change within the cache window, not instantly.
        assert (await RUNTIME.effective(session, settings)) is before
        RUNTIME.invalidate()
        assert (await RUNTIME.effective(session, settings)).ops.max_log_lines == 150

    def test_every_runtime_setting_names_a_real_field(self, settings) -> None:
        for definition in BY_KEY.values():
            group = getattr(settings, definition.group)
            assert definition.field in type(group).model_fields, definition.key
