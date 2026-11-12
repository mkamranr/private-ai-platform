"""Auth hardening (expansion E14): lockout, sign-out, refresh rotation, runner throttle.

Failed sign-ins roll the request back, so counters, locks, alerts and failure audit rows
are committed in their own transactions. Tests that need to see them use
``committed_user`` and read back through its session factory. A committed user is never
given a wrong password after a successful sign-in in the same test: the success holds a
row lock in the still-open test transaction, and the independent failure update would
wait on it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenService
from app.models.audit import AuditAction, AuditLog
from app.models.auth import RevokedToken, User
from app.models.operations import Alert, AlertSeverity, AlertState
from app.repositories.auth_tokens import RevokedTokenRepository
from app.services.login_guard import account_locked_fingerprint, token_reuse_fingerprint
from tests.conftest import TEST_PASSWORD, auth_header

LOGIN = "/api/v1/auth/login"
BAD = {"password": "definitely-the-wrong-one"}


async def _login(client: AsyncClient, username: str, password: str = TEST_PASSWORD):
    return await client.post(LOGIN, json={"username": username, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _alerts(factory, fingerprint: str) -> list[Alert]:
    async with factory() as s:
        rows = await s.execute(select(Alert).where(Alert.fingerprint == fingerprint))
        return list(rows.scalars().all())


async def _audits(factory, action: str, resource_id: str) -> list[AuditLog]:
    async with factory() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.action == action, AuditLog.resource_id == resource_id)
        )
        return list(rows.scalars().all())


@pytest.fixture
def low_lockout(settings, monkeypatch):
    monkeypatch.setattr(settings.auth, "lockout_threshold", 3)
    monkeypatch.setattr(settings.auth, "lockout_minutes", 15)
    return settings


class TestAccountLockout:
    async def test_failures_lock_the_account_audit_and_alert(
        self, client: AsyncClient, committed_user, low_lockout
    ) -> None:
        user, factory = committed_user
        for _ in range(3):
            r = await _login(client, user.username, BAD["password"])
            assert r.status_code == 401

        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None
            assert row.locked_until is not None
            assert row.locked_until > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=14)
            assert row.failed_login_count == 0  # locking resets the count

        locked = await _audits(factory, AuditAction.USER_LOCKED, str(user.id))
        assert len(locked) == 1
        assert locked[0].meta["failures"] == 3
        alerts = await _alerts(factory, account_locked_fingerprint(user.id))
        assert len(alerts) == 1 and alerts[0].state == AlertState.ACTIVE
        assert alerts[0].source == "auth"

    async def test_a_locked_account_refuses_the_right_password_with_the_same_message(
        self, client: AsyncClient, committed_user, low_lockout
    ) -> None:
        user, factory = committed_user
        wrong = None
        for _ in range(3):
            wrong = await _login(client, user.username, BAD["password"])
        right = await _login(client, user.username)
        assert right.status_code == 401
        assert wrong is not None
        # The lock must not reveal that this guess was the right one.
        assert right.json()["error"]["message"] == wrong.json()["error"]["message"]

        async with factory() as s:
            rows = await s.execute(
                select(AuditLog).where(
                    AuditLog.username == user.username, AuditLog.message == "Account is locked"
                )
            )
            assert len(rows.scalars().all()) == 1

    async def test_failures_below_the_threshold_do_not_lock(
        self, client: AsyncClient, committed_user, low_lockout
    ) -> None:
        user, factory = committed_user
        for _ in range(2):
            await _login(client, user.username, BAD["password"])
        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None
            assert row.locked_until is None and row.failed_login_count == 2
        assert (await _login(client, user.username)).status_code == 200

    async def test_lockout_threshold_zero_turns_it_off(
        self, client: AsyncClient, committed_user, settings, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings.auth, "lockout_threshold", 0)
        user, factory = committed_user
        for _ in range(4):
            await _login(client, user.username, BAD["password"])
        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None and row.locked_until is None
        assert (await _login(client, user.username)).status_code == 200

    async def test_an_expired_lock_lets_the_user_in_and_clears_it(
        self, client: AsyncClient, committed_user, low_lockout
    ) -> None:
        user, factory = committed_user
        async with factory() as s:
            await s.execute(
                update(User)
                .where(User.id == user.id)
                .values(locked_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
            )
            await s.commit()
        r = await _login(client, user.username)
        assert r.status_code == 200

    async def test_an_administrator_unlocks_early(
        self,
        client: AsyncClient,
        committed_user,
        low_lockout,
        superuser: User,
        tokens: TokenService,
        session: AsyncSession,
    ) -> None:
        user, _factory = committed_user
        for _ in range(3):
            await _login(client, user.username, BAD["password"])

        listed = await client.get(
            f"/api/v1/users/{user.id}", headers=auth_header(tokens, superuser)
        )
        assert listed.status_code == 200
        assert listed.json()["locked_until"] is not None

        r = await client.post(
            f"/api/v1/users/{user.id}/unlock", headers=auth_header(tokens, superuser)
        )
        assert r.status_code == 200, r.text
        assert r.json()["locked_until"] is None and r.json()["failed_login_count"] == 0

        alert = (
            await session.execute(
                select(Alert).where(Alert.fingerprint == account_locked_fingerprint(user.id))
            )
        ).scalar_one()
        assert alert.state != AlertState.ACTIVE
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.USER_UNLOCKED,
                    AuditLog.resource_id == str(user.id),
                )
            )
        ).scalar_one()
        assert audit.meta["was_locked_until"] is not None

        assert (await _login(client, user.username)).status_code == 200

    async def test_unlock_needs_user_manage(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        r = await client.post(
            f"/api/v1/users/{unprivileged_user.id}/unlock",
            headers=auth_header(tokens, unprivileged_user),
        )
        assert r.status_code == 403


class TestAddressThrottle:
    async def test_repeated_failures_from_one_address_are_refused(
        self, client: AsyncClient, committed_user, settings, monkeypatch, app
    ) -> None:
        monkeypatch.setattr(settings.auth, "login_ip_max_failures", 3)
        monkeypatch.setattr(settings.auth, "lockout_threshold", 0)
        user, factory = committed_user
        try:
            await app.state.redis.client.ping()
        except Exception:
            pytest.skip("Valkey is not reachable")

        # Spraying: a different (unknown) account each time still counts per address.
        for i in range(3):
            r = await _login(client, f"ghost-probe-{uuid.uuid4().hex[:6]}-{i}", "x" * 12)
            assert r.status_code == 401

        refused = await _login(client, user.username)  # even the right password
        assert refused.status_code == 429
        assert refused.json()["error"]["details"]["retry_after_seconds"] > 0

        alerts = await _alerts(factory, "login_throttled:127.0.0.1")
        assert alerts and alerts[0].state == AlertState.ACTIVE

    async def test_valkey_down_fails_open(
        self, client: AsyncClient, unprivileged_user: User, settings, monkeypatch, app
    ) -> None:
        monkeypatch.setattr(settings.auth, "login_ip_max_failures", 1)

        class Broken:
            def __getattr__(self, name):
                raise ConnectionError("valkey down")

        monkeypatch.setattr(app.state.redis, "_client", Broken())
        for _ in range(3):
            r = await _login(client, unprivileged_user.username, BAD["password"])
            assert r.status_code == 401
        assert (await _login(client, unprivileged_user.username)).status_code == 200


class TestSignOut:
    async def test_logout_revokes_the_access_and_refresh_tokens(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        me = await client.get("/api/v1/auth/me", headers=_bearer(pair["access_token"]))
        assert me.status_code == 200

        out = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": pair["refresh_token"]},
            headers=_bearer(pair["access_token"]),
        )
        assert out.status_code == 200

        again = await client.get("/api/v1/auth/me", headers=_bearer(pair["access_token"]))
        assert again.status_code == 401
        refreshed = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert refreshed.status_code == 401

    async def test_logout_without_a_body_still_works(
        self, client: AsyncClient, unprivileged_user: User, session: AsyncSession
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        out = await client.post("/api/v1/auth/logout", headers=_bearer(pair["access_token"]))
        assert out.status_code == 200
        rows = (
            await session.execute(
                select(RevokedToken).where(RevokedToken.user_id == unprivileged_user.id)
            )
        ).scalars()
        assert [r.token_type for r in rows] == ["access"]

    async def test_logout_everywhere_ends_every_session(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        first = (await _login(client, unprivileged_user.username)).json()
        second = (await _login(client, unprivileged_user.username)).json()
        r = await client.post("/api/v1/auth/logout-all", headers=_bearer(first["access_token"]))
        assert r.status_code == 200
        for pair in (first, second):
            me = await client.get("/api/v1/auth/me", headers=_bearer(pair["access_token"]))
            assert me.status_code == 401
            ref = await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
            )
            assert ref.status_code == 401
        # A fresh sign-in works again.
        fresh = (await _login(client, unprivileged_user.username)).json()
        me = await client.get("/api/v1/auth/me", headers=_bearer(fresh["access_token"]))
        assert me.status_code == 200

    async def test_an_administrator_signs_a_user_out(
        self,
        client: AsyncClient,
        unprivileged_user: User,
        superuser: User,
        tokens: TokenService,
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        r = await client.post(
            f"/api/v1/users/{unprivileged_user.id}/sessions/revoke",
            headers=auth_header(tokens, superuser),
        )
        assert r.status_code == 200, r.text
        me = await client.get("/api/v1/auth/me", headers=_bearer(pair["access_token"]))
        assert me.status_code == 401

    async def test_changing_the_password_signs_out_other_sessions(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        r = await client.post(
            "/api/v1/users/me/password",
            json={"current_password": TEST_PASSWORD, "new_password": "a-brand-new-password-1"},
            headers=_bearer(pair["access_token"]),
        )
        assert r.status_code == 200, r.text
        me = await client.get("/api/v1/auth/me", headers=_bearer(pair["access_token"]))
        assert me.status_code == 401

    async def test_tokens_issued_before_e14_without_sv_still_work(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        me = await client.get("/api/v1/auth/me", headers=auth_header(tokens, unprivileged_user))
        assert me.status_code == 200


class TestRefreshRotation:
    async def test_each_refresh_token_works_once(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        first = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert first.status_code == 200
        new = first.json()
        assert new["refresh_token"] != pair["refresh_token"]

        # Within the grace period (two tabs refreshing at once): refused, nobody signed out.
        again = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert again.status_code == 401
        me = await client.get("/api/v1/auth/me", headers=_bearer(new["access_token"]))
        assert me.status_code == 200

    async def test_reuse_after_the_grace_period_signs_out_everywhere(
        self, client: AsyncClient, committed_user, session: AsyncSession, tokens: TokenService
    ) -> None:
        user, factory = committed_user
        # Tokens issued directly rather than by signing in: a sign-in updates the user row
        # inside this open test transaction, and the independent sign-out would wait on it.
        refresh = tokens.create_refresh_token(str(user.id), sv=0)
        rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert rotated.status_code == 200
        # The rotation row joined the test transaction; age it past the grace period.
        await session.execute(
            update(RevokedToken)
            .where(RevokedToken.user_id == user.id)
            .values(revoked_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5))
        )
        await session.flush()

        reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert reuse.status_code == 401
        assert "signed out" in reuse.json()["error"]["message"]

        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None and row.session_version == 1
        reused = await _audits(factory, AuditAction.USER_TOKEN_REUSE_DETECTED, str(user.id))
        assert len(reused) == 1
        alerts = await _alerts(factory, token_reuse_fingerprint(user.id))
        assert alerts and alerts[0].severity == AlertSeverity.CRITICAL


class TestRunnerThrottle:
    RUNNER = "a-real-runner-token-for-tests-only"

    @pytest.fixture
    def runner_env(self, settings, monkeypatch, app):
        monkeypatch.setattr(settings.ops, "runner_token", SecretStr(self.RUNNER))
        monkeypatch.setattr(settings.ops, "runner_max_failures", 3)
        return settings

    async def test_wrong_tokens_are_audited_then_the_address_is_refused(
        self, client: AsyncClient, runner_env, database, app
    ) -> None:
        try:
            await app.state.redis.client.ping()
        except Exception:
            pytest.skip("Valkey is not reachable")
        for _ in range(3):
            r = await client.post(
                "/api/v1/ops/runner/claim",
                json={"name": "h"},
                headers={"X-Ops-Runner-Token": "wrong"},
            )
            assert r.status_code == 401

        refused = await client.post(
            "/api/v1/ops/runner/claim",
            json={"name": "h"},
            headers={"X-Ops-Runner-Token": self.RUNNER},
        )
        assert refused.status_code == 429
        assert refused.json()["error"]["details"]["retry_after_seconds"] > 0

        factory = database.sessionmaker
        alerts = await _alerts(factory, "ops_runner_auth:127.0.0.1")
        assert alerts and alerts[0].source == "ops_runner"
        async with factory() as s:
            rows = await s.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.OPS_RUNNER_AUTH_FAILED,
                    AuditLog.timestamp > dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
                )
            )
            assert len(rows.scalars().all()) >= 3


class TestPrune:
    async def test_expired_revocations_are_forgotten(
        self, session: AsyncSession, unprivileged_user: User
    ) -> None:
        repo = RevokedTokenRepository(session)
        now = dt.datetime.now(dt.UTC)
        for jti, expires in (
            ("old-jti", now - dt.timedelta(hours=1)),
            ("live-jti", now + dt.timedelta(hours=1)),
        ):
            await repo.revoke(
                jti=jti,
                user_id=unprivileged_user.id,
                token_type="access",
                reason="logout",
                expires_at=expires,
            )
        assert (
            await repo.revoke(
                jti="live-jti",
                user_id=unprivileged_user.id,
                token_type="access",
                reason="logout",
                expires_at=now,
            )
            is False
        )
        assert await repo.prune(now=now) >= 1
        assert not await repo.is_revoked("old-jti")
        assert await repo.is_revoked("live-jti")
