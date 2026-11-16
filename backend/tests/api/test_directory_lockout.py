"""Directory accounts are locked after repeated wrong passwords, as local ones are (E25).

Before E25 an LDAP account with a platform row was never counted: every guess went on to
the directory, so guessing through the platform could trip the *directory's* lockout for
the real person. Now the platform counts the failures and, once locked, stops asking.

A fake password provider stands in for LDAP. Failures are committed in their own
transactions, so these tests use ``committed_user``, converted to a directory account.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.api.deps import get_auth_providers
from app.core.interfaces.auth_provider import ExternalIdentity, PasswordAuthProvider
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.operations import AlertState
from app.services.login_guard import account_locked_fingerprint
from tests.api.test_auth_hardening import _alerts, _audits, _login, low_lockout  # noqa: F401

RIGHT = "the-directory-password"
WRONG = "not-the-directory-password"


class FakeDirectory(PasswordAuthProvider):
    name = "ldap"
    display_name = "Directory"

    def __init__(self, username: str, subject: str) -> None:
        self.username = username
        self.subject = subject
        self.asked = 0

    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        self.asked += 1
        if username != self.username or password != RIGHT:
            return None
        return ExternalIdentity(
            subject=self.subject,
            username=username,
            email=f"{username}@test.local",
            provider=self.name,
        )

    async def health(self) -> tuple[bool, str]:
        return True, "fake"


@pytest.fixture
async def directory_user(app, committed_user) -> Iterator[tuple[User, object, FakeDirectory]]:
    """``committed_user`` as an LDAP account, and the fake directory that knows it."""
    user, factory = committed_user
    subject = f"uid={user.username}"
    async with factory() as s:
        await s.execute(
            update(User)
            .where(User.id == user.id)
            .values(auth_provider="ldap", external_subject=subject, hashed_password=None)
        )
        await s.commit()
    directory = FakeDirectory(user.username, subject)
    app.dependency_overrides[get_auth_providers] = lambda: [directory]
    yield user, factory, directory
    app.dependency_overrides.pop(get_auth_providers, None)


async def _row(factory, user: User) -> User:
    async with factory() as s:
        row = await s.get(User, user.id)
        assert row is not None
        return row


class TestDirectoryLockout:
    async def test_the_right_password_signs_in(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        user, _, _ = directory_user
        r = await _login(client, user.username, RIGHT)
        assert r.status_code == 200, r.text

    async def test_wrong_passwords_lock_the_account_audit_and_alert(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        user, factory, _ = directory_user
        for _ in range(3):
            assert (await _login(client, user.username, WRONG)).status_code == 401

        row = await _row(factory, user)
        assert row.locked_until is not None
        assert row.locked_until > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=14)
        (locked,) = await _audits(factory, AuditAction.USER_LOCKED, str(user.id))
        assert locked.meta["failures"] == 3
        (alert,) = await _alerts(factory, account_locked_fingerprint(user.id))
        assert alert.state == AlertState.ACTIVE

    async def test_a_locked_account_is_not_offered_to_the_directory(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        """The point of E25: guesses stop here, not at the directory's own lockout."""
        user, _, directory = directory_user
        wrong = None
        for _ in range(3):
            wrong = await _login(client, user.username, WRONG)
        asked = directory.asked
        right = await _login(client, user.username, RIGHT)
        assert right.status_code == 401
        assert directory.asked == asked, "a locked account's password reached the directory"
        assert wrong is not None
        assert right.json()["error"]["message"] == wrong.json()["error"]["message"]

    async def test_failures_below_the_threshold_do_not_lock(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        user, factory, _ = directory_user
        for _ in range(2):
            await _login(client, user.username, WRONG)
        row = await _row(factory, user)
        assert row.locked_until is None and row.failed_login_count == 2

    async def test_an_expired_lock_lets_the_user_in_and_clears_it(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        user, factory, _ = directory_user
        async with factory() as s:
            await s.execute(
                update(User)
                .where(User.id == user.id)
                .values(locked_until=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1))
            )
            await s.commit()
        r = await _login(client, user.username, RIGHT)
        assert r.status_code == 200, r.text

    async def test_threshold_zero_turns_it_off(
        self, client: AsyncClient, directory_user, settings, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings.auth, "lockout_threshold", 0)
        user, factory, _ = directory_user
        for _ in range(4):
            await _login(client, user.username, WRONG)
        assert (await _row(factory, user)).locked_until is None

    async def test_an_oidc_account_is_not_counted(
        self, client: AsyncClient, directory_user, low_lockout
    ) -> None:
        """No password provider owns it: a password guess at it is not its failure."""
        user, factory, _ = directory_user
        async with factory() as s:
            await s.execute(update(User).where(User.id == user.id).values(auth_provider="oidc"))
            await s.commit()
        for _ in range(3):
            await _login(client, user.username, WRONG)
        row = await _row(factory, user)
        assert row.locked_until is None and row.failed_login_count == 0
