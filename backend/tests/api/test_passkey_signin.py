"""Sign in with a passkey alone: no username, no password, no code step (E30).

:class:`SoftKey` from the E20 tests plays the browser, with real keys and signatures.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.auth import User
from tests.api.test_mfa import _bearer, _enrol
from tests.api.test_passkeys import SoftKey, _b64, _with_passkey, https_site  # noqa: F401
from tests.conftest import TEST_PASSWORD

OPTIONS = "/api/v1/auth/passkey/options"
VERIFY = "/api/v1/auth/passkey/verify"


class DiscoverableKey(SoftKey):
    """A passkey the browser offers unprompted: its answer names the account."""

    def __init__(self, base: SoftKey, user_id: bytes | None, *, verified: bool = True) -> None:
        self.__dict__.update(base.__dict__)
        self.user_id = user_id
        self.verified = verified

    def get(self, public_key: dict[str, Any], *, signer: Any = None) -> dict[str, Any]:
        self.counter += 1
        client = self._client_data("webauthn.get", public_key["challenge"])
        auth_data = self._auth_data(0x05 if self.verified else 0x01)
        signature = (signer or self.key).sign(
            auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client),
                "authenticatorData": _b64(auth_data),
                "signature": _b64(signature),
                "userHandle": _b64(self.user_id) if self.user_id else None,
            },
            "clientExtensionResults": {},
        }


async def _options(client: AsyncClient) -> dict[str, Any]:
    r = await client.post(OPTIONS)
    assert r.status_code == 200, r.text
    return r.json()


async def _sign_in(client: AsyncClient, key: SoftKey, options: dict | None = None):
    body = options or await _options(client)
    return await client.post(
        VERIFY,
        json={
            "challenge_token": body["challenge_token"],
            "credential": key.get(body["public_key"]),
        },
    )


async def _ready(client: AsyncClient, user: User, **kwargs: Any) -> DiscoverableKey:
    _, key = await _with_passkey(client, user)
    return DiscoverableKey(key, user.id.bytes, **kwargs)


class TestOptions:
    async def test_no_account_named_and_verification_required(self, client: AsyncClient) -> None:
        body = await _options(client)
        assert body["public_key"]["allowCredentials"] == []
        assert body["public_key"]["userVerification"] == "required"

    async def test_an_empty_json_body_is_accepted(self, client: AsyncClient) -> None:
        """E33: the shared /js/passkey.js posts ``{}`` from the portal and voice pages."""
        r = await client.post(OPTIONS, json={})
        assert r.status_code == 200, r.text
        assert r.json()["challenge_token"]

    async def test_new_passkeys_are_discoverable_where_possible(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        _, enabled = await _enrol(client, unprivileged_user.username)
        r = await client.post(
            "/api/v1/auth/passkeys/options",
            json={"password": TEST_PASSWORD},
            headers=_bearer(enabled["access_token"]),
        )
        selection = r.json()["public_key"]["authenticatorSelection"]
        assert selection["residentKey"] == "preferred"


class TestSignIn:
    async def test_a_passkey_signs_in_without_password_or_code(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        key = await _ready(client, unprivileged_user)
        r = await _sign_in(client, key)
        assert r.status_code == 200, r.text
        me = await client.get("/api/v1/auth/me", headers=_bearer(r.json()["access_token"]))
        assert me.json()["username"] == unprivileged_user.username

        logins = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.user_id == unprivileged_user.id, AuditLog.action == "USER_LOGIN"
                )
            )
        ).all()
        assert any(row.meta.get("passwordless") is True for row in logins)

    async def test_a_challenge_works_once(self, client: AsyncClient, unprivileged_user) -> None:
        key = await _ready(client, unprivileged_user)
        options = await _options(client)
        assert (await _sign_in(client, key, options)).status_code == 200
        again = await _sign_in(client, key, options)
        assert again.status_code == 401

    async def test_without_user_verification_it_is_refused(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        """Possession alone is one factor: the PIN or biometric is the second."""
        key = await _ready(client, unprivileged_user, verified=False)
        assert (await _sign_in(client, key)).status_code == 401

    async def test_an_unknown_passkey_is_refused(self, client: AsyncClient) -> None:
        stranger = DiscoverableKey(SoftKey(), None)
        r = await _sign_in(client, stranger)
        assert r.status_code == 401
        assert "not accepted" in r.json()["error"]["message"]

    async def test_a_forged_signature_is_refused(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        key = await _ready(client, unprivileged_user)
        body = await _options(client)
        forged = key.get(body["public_key"], signer=ec.generate_private_key(ec.SECP256R1()))
        r = await client.post(
            VERIFY, json={"challenge_token": body["challenge_token"], "credential": forged}
        )
        assert r.status_code == 401

    async def test_a_user_handle_for_someone_else_is_refused(
        self, client: AsyncClient, unprivileged_user, superuser
    ) -> None:
        key = await _ready(client, unprivileged_user)
        key.user_id = superuser.id.bytes
        assert (await _sign_in(client, key)).status_code == 401

    async def test_a_locked_or_disabled_account_is_refused(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        key = await _ready(client, unprivileged_user)
        await session.execute(
            update(User)
            .where(User.id == unprivileged_user.id)
            .values(locked_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10))
        )
        await session.flush()
        assert (await _sign_in(client, key)).status_code == 401

        await session.execute(
            update(User)
            .where(User.id == unprivileged_user.id)
            .values(locked_until=None, is_active=False)
        )
        await session.flush()
        r = await _sign_in(client, key)
        assert r.status_code == 401 and "disabled" in r.json()["error"]["message"]
