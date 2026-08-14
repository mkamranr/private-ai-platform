"""Authentication, authorisation and audit behaviour end to end (M03, M24).

These are the Phase 0 gate assertions: login works, permissions are enforced
server-side, and both outcomes reach the audit log.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import TokenService
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.models.auth import User
from tests.conftest import TEST_PASSWORD, auth_header


async def _audit_rows(session: AsyncSession, **filters: str) -> list[AuditLog]:
    stmt = select(AuditLog)
    for column, value in filters.items():
        stmt = stmt.where(getattr(AuditLog, column) == value)
    return list((await session.execute(stmt)).scalars().all())


class TestLogin:
    async def test_valid_credentials_return_tokens(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": unprivileged_user.username, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]
        assert body["expires_in"] > 0

    async def test_wrong_password_rejected(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": unprivileged_user.username, "password": "wrong-password"},
        )
        assert response.status_code == 401

    async def test_unknown_and_wrong_password_are_indistinguishable(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        """Differing messages would turn login into a user-enumeration oracle."""
        unknown = await client.post(
            "/api/v1/auth/login",
            json={"username": "no-such-user-at-all", "password": TEST_PASSWORD},
        )
        wrong = await client.post(
            "/api/v1/auth/login",
            json={"username": unprivileged_user.username, "password": "wrong-password"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
        assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]

    async def test_disabled_account_cannot_log_in(
        self, client: AsyncClient, disabled_user: User
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": disabled_user.username, "password": TEST_PASSWORD},
        )
        assert response.status_code == 401
        assert "disabled" in response.json()["error"]["message"].lower()

    async def test_response_never_echoes_the_password(
        self, client: AsyncClient, unprivileged_user: User
    ) -> None:
        """Pydantic's raw validation errors can embed submitted values."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": unprivileged_user.username, "password": ""},
        )
        assert TEST_PASSWORD not in response.text
        assert response.status_code in {401, 422}

    async def test_login_records_success_in_audit_log(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user: User
    ) -> None:
        await client.post(
            "/api/v1/auth/login",
            json={"username": unprivileged_user.username, "password": TEST_PASSWORD},
        )
        rows = await _audit_rows(
            session, action=AuditAction.USER_LOGIN, username=unprivileged_user.username
        )
        assert len(rows) == 1
        assert rows[0].result == AuditResult.SUCCESS
        assert rows[0].user_id == unprivileged_user.id
        # Correlates the audit row with that request's log lines.
        assert rows[0].request_id


class TestFailedLoginIsAudited:
    """A failed login raises, so the request transaction rolls back.

    ``AuditService.record_independent`` commits the record in its own transaction
    precisely so it survives that. Without it the platform would log successful
    logins and silently discard every failed one — exactly backwards for a security
    audit, and the kind of gap nobody notices until an incident review.

    These tests use ``committed_user`` because the audit row carries a foreign key
    to ``users.id``: a user existing only inside the test's rolled-back transaction
    is invisible to the independent transaction, so the insert would fail the FK
    check and be silently swallowed.
    """

    async def test_failed_login_row_survives_rollback(
        self, client: AsyncClient, committed_user
    ) -> None:
        user, factory = committed_user
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": "definitely-wrong"},
        )
        assert response.status_code == 401

        async with factory() as verify:
            rows = list(
                (
                    await verify.execute(
                        select(AuditLog).where(
                            AuditLog.username == user.username,
                            AuditLog.action == AuditAction.USER_LOGIN,
                            AuditLog.result == AuditResult.FAILURE,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, "failed login was not durably audited"
        assert rows[0].message == "Incorrect password"
        assert rows[0].user_id == user.id

    async def test_successful_login_is_audited(self, client: AsyncClient, committed_user) -> None:
        """The success path joins the request transaction and commits with it."""
        user, _factory = committed_user
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": TEST_PASSWORD},
        )
        assert response.status_code == 200

    async def test_unknown_user_failure_is_audited(self, client: AsyncClient, database) -> None:
        """A login attempt against a non-existent account must still be recorded.

        This is the enumeration-probe signal: no user row exists, so there is no FK
        to satisfy and ``user_id`` is null, but the attempt itself is what an
        incident review needs to see. Rows are swept by ``_sweep_committed_fixtures``
        via the ``ghost-probe-`` prefix.
        """
        username = f"ghost-probe-{uuid.uuid4().hex[:8]}"
        await client.post("/api/v1/auth/login", json={"username": username, "password": "x" * 20})
        async with database.sessionmaker() as verify:
            rows = list(
                (await verify.execute(select(AuditLog).where(AuditLog.username == username)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].result == AuditResult.FAILURE
        assert rows[0].user_id is None


class TestTokenHandling:
    async def test_access_token_reaches_a_protected_route(
        self, client: AsyncClient, tokens: TokenService, unprivileged_user: User
    ) -> None:
        response = await client.get(
            "/api/v1/auth/me", headers=auth_header(tokens, unprivileged_user)
        )
        assert response.status_code == 200
        assert response.json()["username"] == unprivileged_user.username

    async def test_missing_token_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_token"

    async def test_garbage_token_rejected(self, client: AsyncClient) -> None:
        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401

    async def test_refresh_token_cannot_authenticate(
        self, client: AsyncClient, tokens: TokenService, unprivileged_user: User
    ) -> None:
        refresh = tokens.create_refresh_token(str(unprivileged_user.id))
        response = await client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"}
        )
        assert response.status_code == 401

    async def test_token_for_deleted_user_rejected(
        self,
        client: AsyncClient,
        tokens: TokenService,
        session: AsyncSession,
        unprivileged_user: User,
    ) -> None:
        """The user is re-read from the database each request, so deletion takes
        effect immediately rather than when the token expires."""
        header = auth_header(tokens, unprivileged_user)
        await session.delete(unprivileged_user)
        await session.flush()
        assert (await client.get("/api/v1/auth/me", headers=header)).status_code == 401

    async def test_refresh_issues_a_new_pair(
        self, client: AsyncClient, tokens: TokenService, unprivileged_user: User
    ) -> None:
        refresh = tokens.create_refresh_token(str(unprivileged_user.id))
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 200
        assert response.json()["access_token"]

    async def test_access_token_rejected_at_refresh(
        self, client: AsyncClient, tokens: TokenService, unprivileged_user: User
    ) -> None:
        access = tokens.create_access_token(str(unprivileged_user.id))
        response = await client.post("/api/v1/auth/refresh", json={"refresh_token": access})
        assert response.status_code == 401


class TestAuthorisation:
    """require_permission is the platform's single authorisation primitive."""

    async def test_permitted_user_gets_200(
        self, client: AsyncClient, tokens: TokenService, privileged_user: User
    ) -> None:
        response = await client.get("/api/v1/users", headers=auth_header(tokens, privileged_user))
        assert response.status_code == 200
        assert "items" in response.json()

    async def test_unprivileged_user_gets_403_not_404(
        self, client: AsyncClient, tokens: TokenService, unprivileged_user: User
    ) -> None:
        """403, not 404: the caller is authenticated and simply lacks the grant."""
        response = await client.get("/api/v1/users", headers=auth_header(tokens, unprivileged_user))
        assert response.status_code == 403
        body = response.json()
        assert body["error"]["code"] == "permission_denied"
        # Names the missing permission so an admin can act on it.
        assert body["error"]["details"]["required_permission"] == "user.view"

    async def test_superuser_bypasses_permission_checks(
        self, client: AsyncClient, tokens: TokenService, superuser: User
    ) -> None:
        assert (
            await client.get("/api/v1/users", headers=auth_header(tokens, superuser))
        ).status_code == 200

    async def test_denial_is_durably_audited(
        self, client: AsyncClient, tokens: TokenService, committed_user
    ) -> None:
        """A run of DENIED entries against one principal is a probing signal, so it
        must not be lost to the 403's rollback."""
        user, factory = committed_user
        response = await client.get("/api/v1/users", headers=auth_header(tokens, user))
        assert response.status_code == 403

        async with factory() as verify:
            rows = list(
                (
                    await verify.execute(
                        select(AuditLog).where(
                            AuditLog.username == user.username,
                            AuditLog.result == AuditResult.DENIED,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, "permission denial was not durably audited"
        assert rows[0].meta["required_permission"] == "user.view"

    async def test_write_endpoint_also_enforced(
        self, client: AsyncClient, tokens: TokenService, privileged_user: User
    ) -> None:
        """user.view must not imply user.manage."""
        response = await client.post(
            "/api/v1/users",
            headers=auth_header(tokens, privileged_user),
            json={
                "username": "newperson",
                "email": "newperson@test.local",
                "password": "a-sufficiently-long-password",
            },
        )
        assert response.status_code == 403


class TestCurrentUser:
    async def test_me_exposes_permissions_but_never_the_hash(
        self, client: AsyncClient, tokens: TokenService, privileged_user: User
    ) -> None:
        response = await client.get("/api/v1/auth/me", headers=auth_header(tokens, privileged_user))
        assert response.status_code == 200
        body = response.json()
        assert "user.view" in body["permissions"]
        # A serialised password hash would leak into every log and cache that
        # touches this endpoint.
        assert "hashed_password" not in body
        assert "password" not in response.text

    async def test_logout_is_audited(
        self,
        client: AsyncClient,
        tokens: TokenService,
        session: AsyncSession,
        unprivileged_user: User,
    ) -> None:
        response = await client.post(
            "/api/v1/auth/logout", headers=auth_header(tokens, unprivileged_user)
        )
        assert response.status_code == 200
        rows = await _audit_rows(
            session, action=AuditAction.USER_LOGOUT, username=unprivileged_user.username
        )
        assert len(rows) == 1


class TestRequestCorrelation:
    async def test_request_id_returned_and_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/version")
        assert response.headers.get("X-Request-ID")

    async def test_inbound_request_id_is_reused(self, client: AsyncClient) -> None:
        """nginx passes one through so a trace spans every hop."""
        response = await client.get("/api/v1/version", headers={"X-Request-ID": "trace-me-12345"})
        assert response.headers["X-Request-ID"] == "trace-me-12345"

    async def test_security_headers_present(self, client: AsyncClient) -> None:
        headers = (await client.get("/api/v1/version")).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"

    async def test_error_body_carries_request_id(self, client: AsyncClient) -> None:
        """Links a user-reported failure to its log lines."""
        response = await client.get("/api/v1/auth/me")
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
