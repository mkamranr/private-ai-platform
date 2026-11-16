"""Passkeys and trusted browsers for the two-factor step (E20).

The browser side is played by :class:`SoftKey`, a software authenticator: a real P-256
key, a real "none" attestation and real assertion signatures, so the server is checked
by the same verification a hardware key would face.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from typing import Any

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.security import SecretCipher
from app.models.audit import AuditLog
from app.models.auth import TrustedDevice, User, UserPasskey
from tests.api.test_mfa import _bearer, _code, _enrol, _login
from tests.conftest import TEST_PASSWORD

ORIGIN = "https://ai.example.test"
RP_ID = "ai.example.test"
VERIFY = "/api/v1/auth/mfa/verify"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftKey:
    """A software authenticator: what a security key does, in a few lines."""

    def __init__(self, *, origin: str = ORIGIN, rp_id: str = RP_ID) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.counter = 0
        self.origin = origin
        self.rp_id = rp_id

    def cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, flags: int, *, attested: bool = False) -> bytes:
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags])
        data += self.counter.to_bytes(4, "big")
        if attested:
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id
            data += self.cose_key()
        return data

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}
        ).encode()

    def create(self, public_key: dict[str, Any]) -> dict[str, Any]:
        client = self._client_data("webauthn.create", public_key["challenge"])
        attestation = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": self._auth_data(0x45, attested=True)}
        )
        return {
            "id": _b64(self.credential_id),
            "rawId": _b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64(client),
                "attestationObject": _b64(attestation),
                "transports": ["usb"],
            },
            "clientExtensionResults": {},
        }

    def get(self, public_key: dict[str, Any], *, signer: Any = None) -> dict[str, Any]:
        self.counter += 1
        client = self._client_data("webauthn.get", public_key["challenge"])
        auth_data = self._auth_data(0x05)
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
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


@pytest.fixture(autouse=True)
def https_site(settings, monkeypatch):
    """Passkeys need a host name over HTTPS: give the platform one."""
    monkeypatch.setattr(settings.platform, "public_base_url", ORIGIN)
    monkeypatch.setattr(settings.auth, "webauthn_rp_id", "")
    monkeypatch.setattr(settings.auth, "webauthn_origins", [])


async def _add_passkey(client: AsyncClient, access: str, key: SoftKey, name: str = "YubiKey"):
    options = await client.post(
        "/api/v1/auth/passkeys/options", json={"password": TEST_PASSWORD}, headers=_bearer(access)
    )
    assert options.status_code == 200, options.text
    body = options.json()
    assert body["public_key"]["rp"]["id"] == RP_ID
    return await client.post(
        "/api/v1/auth/passkeys",
        json={
            "challenge_token": body["challenge_token"],
            "credential": key.create(body["public_key"]),
            "name": name,
        },
        headers=_bearer(access),
    )


async def _with_passkey(client: AsyncClient, user: User) -> tuple[str, SoftKey]:
    """MFA on, plus one registered passkey. Returns the TOTP secret and the key."""
    secret, enabled = await _enrol(client, user.username)
    key = SoftKey()
    added = await _add_passkey(client, enabled["access_token"], key)
    assert added.status_code == 201, added.text
    return secret, key


async def _passkey_step(
    client: AsyncClient,
    user: User,
    key: SoftKey,
    *,
    signer: Any = None,
    remember: bool = False,
):
    challenge = (await _login(client, user.username)).json()
    assert challenge["mfa_required"] and challenge["passkey_available"] is True
    options = await client.post(
        "/api/v1/auth/mfa/passkey/options", json={"mfa_token": challenge["mfa_token"]}
    )
    assert options.status_code == 200, options.text
    body = options.json()
    return (
        challenge,
        body,
        await client.post(
            "/api/v1/auth/mfa/passkey/verify",
            json={
                "mfa_token": challenge["mfa_token"],
                "challenge_token": body["challenge_token"],
                "credential": key.get(body["public_key"], signer=signer),
                "remember_device": remember,
            },
        ),
    )


class TestRegistration:
    async def test_needs_two_factor_first(self, client: AsyncClient, unprivileged_user) -> None:
        access = (await _login(client, unprivileged_user.username)).json()["access_token"]
        r = await client.post(
            "/api/v1/auth/passkeys/options",
            json={"password": TEST_PASSWORD},
            headers=_bearer(access),
        )
        assert r.status_code == 422
        assert "authenticator app first" in r.json()["error"]["message"]

    async def test_needs_the_password(self, client: AsyncClient, unprivileged_user) -> None:
        _, enabled = await _enrol(client, unprivileged_user.username)
        r = await client.post(
            "/api/v1/auth/passkeys/options",
            json={"password": "not-the-password"},
            headers=_bearer(enabled["access_token"]),
        )
        assert r.status_code == 422

    async def test_add_list_remove(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        _, enabled = await _enrol(client, unprivileged_user.username)
        access = enabled["access_token"]
        added = await _add_passkey(client, access, SoftKey(), name="Work key")
        assert added.status_code == 201, added.text
        assert added.json()["name"] == "Work key" and added.json()["transports"] == ["usb"]

        listed = (await client.get("/api/v1/auth/passkeys", headers=_bearer(access))).json()
        assert [p["name"] for p in listed] == ["Work key"]
        status = (await client.get("/api/v1/auth/mfa", headers=_bearer(access))).json()
        assert status["passkeys_supported"] is True and status["passkeys"] == 1

        removed = await client.delete(
            f"/api/v1/auth/passkeys/{listed[0]['id']}", headers=_bearer(access)
        )
        assert removed.status_code == 200
        assert (await client.get("/api/v1/auth/passkeys", headers=_bearer(access))).json() == []
        actions = (
            await session.scalars(
                select(AuditLog.action).where(AuditLog.resource_id == str(unprivileged_user.id))
            )
        ).all()
        assert {"USER_PASSKEY_ADDED", "USER_PASSKEY_REMOVED"} <= set(actions)

    async def test_a_challenge_works_once(self, client: AsyncClient, unprivileged_user) -> None:
        _, enabled = await _enrol(client, unprivileged_user.username)
        access = enabled["access_token"]
        body = (
            await client.post(
                "/api/v1/auth/passkeys/options",
                json={"password": TEST_PASSWORD},
                headers=_bearer(access),
            )
        ).json()
        first = await client.post(
            "/api/v1/auth/passkeys",
            json={
                "challenge_token": body["challenge_token"],
                "credential": SoftKey().create(body["public_key"]),
                "name": "one",
            },
            headers=_bearer(access),
        )
        assert first.status_code == 201
        again = await client.post(
            "/api/v1/auth/passkeys",
            json={
                "challenge_token": body["challenge_token"],
                "credential": SoftKey().create(body["public_key"]),
                "name": "two",
            },
            headers=_bearer(access),
        )
        assert again.status_code == 401

    async def test_a_wrong_origin_is_refused(self, client: AsyncClient, unprivileged_user) -> None:
        _, enabled = await _enrol(client, unprivileged_user.username)
        r = await _add_passkey(
            client, enabled["access_token"], SoftKey(origin="https://evil.example")
        )
        assert r.status_code == 422

    async def test_a_site_reached_by_ip_has_no_passkeys(
        self, client: AsyncClient, settings, monkeypatch, unprivileged_user
    ) -> None:
        monkeypatch.setattr(settings.platform, "public_base_url", "http://10.0.0.5:8080")
        _, enabled = await _enrol(client, unprivileged_user.username)
        access = enabled["access_token"]
        status = (await client.get("/api/v1/auth/mfa", headers=_bearer(access))).json()
        assert status["passkeys_supported"] is False
        r = await client.post(
            "/api/v1/auth/passkeys/options",
            json={"password": TEST_PASSWORD},
            headers=_bearer(access),
        )
        assert r.status_code == 409
        assert "host name over HTTPS" in r.json()["error"]["message"]


class TestSignIn:
    async def test_a_passkey_answers_the_step(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        _, key = await _with_passkey(client, unprivileged_user)
        _, _, result = await _passkey_step(client, unprivileged_user, key)
        assert result.status_code == 200, result.text
        me = await client.get("/api/v1/auth/me", headers=_bearer(result.json()["access_token"]))
        assert me.status_code == 200
        passkey = (
            await session.scalars(
                select(UserPasskey).where(UserPasskey.user_id == unprivileged_user.id)
            )
        ).one()
        await session.refresh(passkey)
        assert passkey.sign_count == 1 and passkey.last_used_at is not None
        # Every row in the test transaction shares one now(), so "the latest" is not
        # well defined: look for the passkey sign-in among them.
        logins = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "USER_LOGIN", AuditLog.user_id == unprivileged_user.id
                )
            )
        ).all()
        assert any(row.meta.get("mfa") == "passkey" for row in logins)

    async def test_a_code_still_works(self, client: AsyncClient, unprivileged_user) -> None:
        secret, _ = await _with_passkey(client, unprivileged_user)
        challenge = (await _login(client, unprivileged_user.username)).json()
        r = await client.post(
            VERIFY, json={"mfa_token": challenge["mfa_token"], "code": _code(secret, 1)}
        )
        assert r.status_code == 200, r.text

    async def test_a_wrong_signature_counts_as_a_failure(
        self, client: AsyncClient, committed_user, settings
    ) -> None:
        """Counted in an independent transaction, so the user (and key) are committed."""
        user, factory = committed_user
        key = SoftKey()
        async with factory() as s:
            await s.execute(
                update(User)
                .where(User.id == user.id)
                .values(
                    mfa_secret_encrypted=SecretCipher(settings.security).encrypt(totp.new_secret()),
                    mfa_enabled_at=dt.datetime.now(dt.UTC),
                    mfa_recovery_hashes=[],
                )
            )
            s.add(
                UserPasskey(
                    user_id=user.id,
                    credential_id=key.credential_id,
                    public_key=key.cose_key(),
                    sign_count=0,
                    transports=[],
                    name="key",
                    backed_up=False,
                )
            )
            await s.commit()
        impostor = ec.generate_private_key(ec.SECP256R1())
        _, _, result = await _passkey_step(client, user, key, signer=impostor)
        assert result.status_code == 401
        assert "passkey was not accepted" in result.json()["error"]["message"]
        async with factory() as s:
            row = await s.get(User, user.id)
            assert row is not None and row.failed_login_count == 1

    async def test_a_cloned_key_is_refused(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        """The signature counter went backwards: someone else holds a copy."""
        _, key = await _with_passkey(client, unprivileged_user)
        await session.execute(
            update(UserPasskey)
            .where(UserPasskey.user_id == unprivileged_user.id)
            .values(sign_count=50)
        )
        await session.flush()
        _, _, result = await _passkey_step(client, unprivileged_user, key)
        assert result.status_code == 401

    async def test_the_step_cannot_be_completed_twice(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        _, key = await _with_passkey(client, unprivileged_user)
        challenge, body, first = await _passkey_step(client, unprivileged_user, key)
        assert first.status_code == 200
        again = await client.post(
            "/api/v1/auth/mfa/passkey/verify",
            json={
                "mfa_token": challenge["mfa_token"],
                "challenge_token": body["challenge_token"],
                "credential": key.get(body["public_key"]),
            },
        )
        assert again.status_code == 401

    async def test_a_challenge_belongs_to_its_own_step(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        _, key = await _with_passkey(client, unprivileged_user)
        first = (await _login(client, unprivileged_user.username)).json()
        options = (
            await client.post(
                "/api/v1/auth/mfa/passkey/options", json={"mfa_token": first["mfa_token"]}
            )
        ).json()
        second = (await _login(client, unprivileged_user.username)).json()
        r = await client.post(
            "/api/v1/auth/mfa/passkey/verify",
            json={
                "mfa_token": second["mfa_token"],
                "challenge_token": options["challenge_token"],
                "credential": key.get(options["public_key"]),
            },
        )
        assert r.status_code == 401
        assert "another sign-in" in r.json()["error"]["message"]

    async def test_no_passkey_means_no_offer(self, client: AsyncClient, unprivileged_user) -> None:
        await _enrol(client, unprivileged_user.username)
        challenge = (await _login(client, unprivileged_user.username)).json()
        assert challenge["passkey_available"] is False
        r = await client.post(
            "/api/v1/auth/mfa/passkey/options", json={"mfa_token": challenge["mfa_token"]}
        )
        assert r.status_code == 422


class TestTrustedBrowsers:
    async def _trusted(self, client: AsyncClient, user: User) -> tuple[str, str]:
        secret, _ = await _enrol(client, user.username)
        challenge = (await _login(client, user.username)).json()
        assert challenge["remember_days"] == 30
        r = await client.post(
            VERIFY,
            json={
                "mfa_token": challenge["mfa_token"],
                "code": _code(secret, 1),
                "remember_device": True,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["device_token"].startswith("tdv_")
        assert r.json()["device_expires_at"]
        return secret, r.json()["device_token"]

    async def _login_with(self, client: AsyncClient, user: User, device: str | None):
        return await client.post(
            "/api/v1/auth/login",
            json={"username": user.username, "password": TEST_PASSWORD, "device_token": device},
        )

    async def test_a_trusted_browser_skips_the_step(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        r = await self._login_with(client, unprivileged_user, device)
        assert r.status_code == 200 and "access_token" in r.json(), r.text
        stored = (
            await session.scalars(
                select(TrustedDevice).where(TrustedDevice.user_id == unprivileged_user.id)
            )
        ).one()
        assert stored.token_hash != device and len(stored.token_hash) == 64

    async def test_the_password_is_still_checked(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        r = await client.post(
            "/api/v1/auth/login",
            json={
                "username": unprivileged_user.username,
                "password": "wrong",
                "device_token": device,
            },
        )
        assert r.status_code == 401

    async def test_another_accounts_token_does_not_count(
        self, client: AsyncClient, unprivileged_user, superuser
    ) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        await _enrol(client, superuser.username)
        r = await self._login_with(client, superuser, device)
        assert r.json().get("mfa_required") is True

    async def test_signing_out_everywhere_forgets_trusted_browsers(
        self, client: AsyncClient, unprivileged_user
    ) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        access = (await self._login_with(client, unprivileged_user, device)).json()["access_token"]
        assert (
            await client.post("/api/v1/auth/logout-all", headers=_bearer(access))
        ).status_code == 200
        r = await self._login_with(client, unprivileged_user, device)
        assert r.json().get("mfa_required") is True

    async def test_turning_the_option_off_stops_honouring_tokens(
        self, client: AsyncClient, settings, monkeypatch, unprivileged_user
    ) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        monkeypatch.setattr(settings.auth, "mfa_remember_days", 0)
        r = await self._login_with(client, unprivileged_user, device)
        assert r.json().get("mfa_required") is True and r.json()["remember_days"] == 0

    async def test_forgetting_a_browser(self, client: AsyncClient, unprivileged_user) -> None:
        _, device = await self._trusted(client, unprivileged_user)
        access = (await self._login_with(client, unprivileged_user, device)).json()["access_token"]
        listed = (await client.get("/api/v1/auth/trusted-devices", headers=_bearer(access))).json()
        assert len(listed) == 1 and listed[0]["label"]
        forgot = await client.delete(
            f"/api/v1/auth/trusted-devices/{listed[0]['id']}", headers=_bearer(access)
        )
        assert forgot.status_code == 200
        r = await self._login_with(client, unprivileged_user, device)
        assert r.json().get("mfa_required") is True

    async def test_turning_mfa_off_removes_passkeys_and_browsers(
        self, client: AsyncClient, session: AsyncSession, unprivileged_user
    ) -> None:
        secret, key = await _with_passkey(client, unprivileged_user)
        _, _, step = await _passkey_step(client, unprivileged_user, key, remember=True)
        signed = step.json()
        assert signed["device_token"]
        off = await client.post(
            "/api/v1/auth/mfa/disable",
            json={"password": TEST_PASSWORD, "code": _code(secret, 1)},
            headers=_bearer(signed["access_token"]),
        )
        assert off.status_code == 200, off.text
        for model in (UserPasskey, TrustedDevice):
            rows = (
                await session.scalars(select(model).where(model.user_id == unprivileged_user.id))
            ).all()
            assert rows == [], model.__name__
