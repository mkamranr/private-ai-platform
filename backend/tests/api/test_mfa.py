"""Two-factor authentication (expansion E15).

Codes are computed from the secret the set-up returns, one time step ahead of the code
used to enable, so each is accepted once (a code's step can never be reused).

Lockout tests use ``committed_user`` with MFA switched on directly in the database: a
wrong code is counted in an independent transaction, which must neither be invisible to
the user row nor wait on a lock the test transaction holds.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.security import SecretCipher, TokenService
from app.models.audit import AuditAction, AuditLog
from app.models.auth import User
from tests.conftest import TEST_PASSWORD, auth_header

LOGIN = "/api/v1/auth/login"
VERIFY = "/api/v1/auth/mfa/verify"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _code(secret: str, ahead: int = 0) -> str:
    return totp.code_at(secret, totp.current_step() + ahead)


async def _login(client: AsyncClient, username: str, password: str = TEST_PASSWORD):
    return await client.post(LOGIN, json={"username": username, "password": password})


async def _enrol(client: AsyncClient, username: str) -> tuple[str, dict]:
    """Turn MFA on through the API. Returns the secret and the enable response."""
    pair = (await _login(client, username)).json()
    setup = await client.post(
        "/api/v1/auth/mfa/setup",
        json={"password": TEST_PASSWORD},
        headers=_bearer(pair["access_token"]),
    )
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    enabled = await client.post(
        "/api/v1/auth/mfa/enable",
        json={"code": _code(secret, -1)},
        headers=_bearer(pair["access_token"]),
    )
    assert enabled.status_code == 200, enabled.text
    return secret, enabled.json()


@pytest.fixture
def mfa_policy(settings, monkeypatch):
    def set_policy(value: str) -> None:
        monkeypatch.setattr(settings.auth, "mfa_required", value)

    return set_policy


class TestEnrolment:
    async def test_setup_returns_a_secret_uri_and_qr_code(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        r = await client.post(
            "/api/v1/auth/mfa/setup",
            json={"password": TEST_PASSWORD},
            headers=_bearer(pair["access_token"]),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["otpauth_uri"].startswith("otpauth://totp/")
        assert body["secret"] in body["otpauth_uri"]
        assert body["qr_svg"].startswith("<svg")
        # Not on until a code confirms it.
        status = await client.get("/api/v1/auth/mfa", headers=_bearer(pair["access_token"]))
        assert status.json()["enabled"] is False
        assert "access_token" in (await _login(client, unprivileged_user.username)).json()

    async def test_setup_needs_the_password(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        r = await client.post(
            "/api/v1/auth/mfa/setup",
            json={"password": "not-the-password"},
            headers=auth_header(tokens, unprivileged_user),
        )
        assert r.status_code == 422

    async def test_the_secret_is_stored_encrypted(
        self, client: AsyncClient, unprivileged_user: User, session: AsyncSession
    ) -> None:
        secret, _ = await _enrol(client, unprivileged_user.username)
        await session.refresh(unprivileged_user)
        assert unprivileged_user.mfa_secret_encrypted
        assert secret not in unprivileged_user.mfa_secret_encrypted
        assert len(unprivileged_user.mfa_recovery_hashes) == 10

    async def test_a_wrong_first_code_does_not_enable(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        pair = (await _login(client, unprivileged_user.username)).json()
        await client.post(
            "/api/v1/auth/mfa/setup",
            json={"password": TEST_PASSWORD},
            headers=_bearer(pair["access_token"]),
        )
        r = await client.post(
            "/api/v1/auth/mfa/enable",
            json={"code": "000000"},
            headers=_bearer(pair["access_token"]),
        )
        assert r.status_code == 422

    async def test_enabling_returns_recovery_codes_and_signs_out_other_sessions(
        self, client: AsyncClient, unprivileged_user: User, session: AsyncSession
    ) -> None:
        other = (await _login(client, unprivileged_user.username)).json()
        _secret, enabled = await _enrol(client, unprivileged_user.username)
        assert len(enabled["recovery_codes"]) == 10
        stale = await client.get("/api/v1/auth/me", headers=_bearer(other["access_token"]))
        assert stale.status_code == 401
        fresh = await client.get("/api/v1/auth/me", headers=_bearer(enabled["access_token"]))
        assert fresh.status_code == 200 and fresh.json()["mfa_enabled"] is True
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.USER_MFA_ENABLED,
                    AuditLog.resource_id == str(unprivileged_user.id),
                )
            )
        ).scalar_one()
        assert audit.user_id == unprivileged_user.id


class TestSignIn:
    async def test_a_correct_password_returns_a_challenge_not_tokens(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        await _enrol(client, unprivileged_user.username)
        r = await _login(client, unprivileged_user.username)
        assert r.status_code == 200
        body = r.json()
        assert body["mfa_required"] is True and body["enrolment_required"] is False
        assert "access_token" not in body
        assert 0 < body["expires_in"] <= 1800

    async def test_the_code_completes_sign_in_once(
        self, client: AsyncClient, unprivileged_user: User, session: AsyncSession
    ) -> None:
        secret, _ = await _enrol(client, unprivileged_user.username)
        challenge = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        r = await client.post(VERIFY, json={"mfa_token": challenge, "code": _code(secret)})
        assert r.status_code == 200, r.text
        me = await client.get("/api/v1/auth/me", headers=_bearer(r.json()["access_token"]))
        assert me.status_code == 200

        # The step token is spent.
        again = await client.post(VERIFY, json={"mfa_token": challenge, "code": _code(secret, 1)})
        assert again.status_code == 401

        # And the code itself cannot be replayed with a fresh step token.
        challenge2 = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        replay = await client.post(VERIFY, json={"mfa_token": challenge2, "code": _code(secret)})
        assert replay.status_code == 401

        logins = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.USER_LOGIN,
                    AuditLog.user_id == unprivileged_user.id,
                )
            )
        ).scalars()
        assert any(row.meta.get("mfa") == "totp" for row in logins)

    async def test_a_recovery_code_works_once(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        _secret, enabled = await _enrol(client, unprivileged_user.username)
        code = enabled["recovery_codes"][0]
        first = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        r = await client.post(VERIFY, json={"mfa_token": first, "code": code.upper()})
        assert r.status_code == 200, r.text
        status = await client.get("/api/v1/auth/mfa", headers=_bearer(r.json()["access_token"]))
        assert status.json()["recovery_codes_left"] == 9

        second = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        reused = await client.post(VERIFY, json={"mfa_token": second, "code": code})
        assert reused.status_code == 401

    async def test_step_tokens_and_access_tokens_are_not_interchangeable(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        secret, enabled = await _enrol(client, unprivileged_user.username)
        challenge = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        as_access = await client.get("/api/v1/auth/me", headers=_bearer(challenge))
        assert as_access.status_code == 401
        as_step = await client.post(
            VERIFY, json={"mfa_token": enabled["access_token"], "code": _code(secret)}
        )
        assert as_step.status_code == 401

    async def test_an_expired_step_token_is_refused(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        secret, _ = await _enrol(client, unprivileged_user.username)
        expired = tokens.create_mfa_token(str(unprivileged_user.id), -1, sv=1, purpose="verify")
        r = await client.post(VERIFY, json={"mfa_token": expired, "code": _code(secret)})
        assert r.status_code == 401


class TestLockout:
    async def _with_mfa(self, committed_user, settings) -> tuple[User, object, str]:
        user, factory = committed_user
        secret = totp.new_secret()
        async with factory() as s:
            await s.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    mfa_secret_encrypted=SecretCipher(settings.security).encrypt(secret),
                    mfa_enabled_at=dt.datetime.now(dt.UTC),
                    mfa_recovery_hashes=[],
                )
            )
            await s.commit()
        return user, factory, secret

    async def test_wrong_codes_count_toward_the_lockout(
        self, client: AsyncClient, committed_user, settings, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings.auth, "lockout_threshold", 3)
        user, factory, secret = await self._with_mfa(committed_user, settings)

        challenge = (await _login(client, user.username)).json()["mfa_token"]
        for _ in range(2):
            r = await client.post(VERIFY, json={"mfa_token": challenge, "code": "000000"})
            assert r.status_code == 401
        # A correct password in between must not reset the count.
        challenge = (await _login(client, user.username)).json()["mfa_token"]
        r = await client.post(VERIFY, json={"mfa_token": challenge, "code": "000000"})
        assert r.status_code == 401

        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None and row.locked_until is not None

        # Locked: the password is refused with the generic message, the code too.
        assert (await _login(client, user.username)).status_code == 401
        right = await client.post(VERIFY, json={"mfa_token": challenge, "code": _code(secret)})
        assert right.status_code == 401
        assert right.json()["error"]["message"] == r.json()["error"]["message"]


class TestPolicy:
    async def test_admins_must_enrol_at_sign_in(
        self, client: AsyncClient, superuser: User, unprivileged_user: User, mfa_policy
    ) -> None:
        mfa_policy("admins")
        assert "access_token" in (await _login(client, unprivileged_user.username)).json()

        body = (await _login(client, superuser.username)).json()
        assert body["mfa_required"] is True and body["enrolment_required"] is True
        start = await client.post(
            "/api/v1/auth/mfa/enrol/start", json={"mfa_token": body["mfa_token"]}
        )
        assert start.status_code == 200, start.text
        secret = start.json()["secret"]

        # The enrolment token cannot be spent at the verify step.
        wrong_step = await client.post(
            VERIFY, json={"mfa_token": body["mfa_token"], "code": _code(secret)}
        )
        assert wrong_step.status_code == 401

        finish = await client.post(
            "/api/v1/auth/mfa/enrol/finish",
            json={"mfa_token": body["mfa_token"], "code": _code(secret)},
        )
        assert finish.status_code == 200, finish.text
        assert len(finish.json()["recovery_codes"]) == 10
        me = await client.get("/api/v1/auth/me", headers=_bearer(finish.json()["access_token"]))
        assert me.json()["mfa_enabled"] is True

        # Next sign-in asks for a code rather than a set-up.
        nxt = (await _login(client, superuser.username)).json()
        assert nxt["mfa_required"] is True and nxt["enrolment_required"] is False

    async def test_all_covers_everyone(
        self, client: AsyncClient, unprivileged_user: User, mfa_policy
    ) -> None:
        mfa_policy("all")
        body = (await _login(client, unprivileged_user.username)).json()
        assert body.get("enrolment_required") is True

    async def test_mfa_cannot_be_turned_off_while_required(
        self, client: AsyncClient, unprivileged_user: User, mfa_policy
    ) -> None:
        secret, enabled = await _enrol(client, unprivileged_user.username)
        mfa_policy("all")
        r = await client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": TEST_PASSWORD, "code": _code(secret)},
            headers=_bearer(enabled["access_token"]),
        )
        assert r.status_code == 403

    async def test_the_runtime_setting_rejects_unknown_values(
        self, client: AsyncClient, superuser: User, tokens: TokenService
    ) -> None:
        r = await client.put(
            "/api/v1/settings/auth.mfa_required",
            json={"value": "sometimes"},
            headers=auth_header(tokens, superuser),
        )
        assert r.status_code == 422, r.text


class TestSelfService:
    async def test_disable_needs_password_and_code(
        self, client: AsyncClient, unprivileged_user: User, session: AsyncSession
    ) -> None:
        secret, enabled = await _enrol(client, unprivileged_user.username)
        headers = _bearer(enabled["access_token"])
        bad = await client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": TEST_PASSWORD, "code": "000000"},
            headers=headers,
        )
        assert bad.status_code == 422
        ok = await client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": TEST_PASSWORD, "code": _code(secret)},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text
        assert "access_token" in (await _login(client, unprivileged_user.username)).json()
        await session.refresh(unprivileged_user)
        assert unprivileged_user.mfa_secret_encrypted is None

    async def test_regenerating_recovery_codes_retires_the_old_ones(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        secret, enabled = await _enrol(client, unprivileged_user.username)
        r = await client.post(
            "/api/v1/auth/mfa/recovery-codes",
            json={"code": _code(secret)},
            headers=_bearer(enabled["access_token"]),
        )
        assert r.status_code == 200, r.text
        new = r.json()["recovery_codes"]
        assert set(new).isdisjoint(enabled["recovery_codes"])
        challenge = (await _login(client, unprivileged_user.username)).json()["mfa_token"]
        old = await client.post(
            VERIFY, json={"mfa_token": challenge, "code": enabled["recovery_codes"][0]}
        )
        assert old.status_code == 401


class TestAdminReset:
    async def test_an_administrator_removes_mfa_and_signs_the_user_out(
        self,
        client: AsyncClient,
        unprivileged_user: User,
        superuser: User,
        tokens: TokenService,
        session: AsyncSession,
    ) -> None:
        _secret, enabled = await _enrol(client, unprivileged_user.username)
        r = await client.post(
            f"/api/v1/users/{unprivileged_user.id}/mfa/reset",
            headers=auth_header(tokens, superuser),
        )
        assert r.status_code == 200, r.text
        assert r.json()["mfa_enabled"] is False
        stale = await client.get("/api/v1/auth/me", headers=_bearer(enabled["access_token"]))
        assert stale.status_code == 401
        assert "access_token" in (await _login(client, unprivileged_user.username)).json()
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == AuditAction.USER_MFA_RESET,
                    AuditLog.resource_id == str(unprivileged_user.id),
                )
            )
        ).scalar_one()
        assert audit.meta["was_enabled"] is True

    async def test_reset_needs_user_manage(
        self, client: AsyncClient, unprivileged_user: User, tokens: TokenService
    ) -> None:
        r = await client.post(
            f"/api/v1/users/{unprivileged_user.id}/mfa/reset",
            headers=auth_header(tokens, unprivileged_user),
        )
        assert r.status_code == 403
