"""Passkeys and security keys as a second factor (expansion E20).

A passkey answers the two-factor step instead of a six-digit code. It does not replace
the password, and it is added only once two-factor authentication is on, so the
authenticator app and the recovery codes stay as the way back in when a key is lost.

**Relying party.** WebAuthn binds a credential to a host name (the relying party id) and
checks the page's origin. Both come from ``PLATFORM__PUBLIC_BASE_URL`` unless
``AUTH__WEBAUTHN_RP_ID`` / ``AUTH__WEBAUTHN_ORIGINS`` say otherwise. Browsers refuse an IP
address as a relying party id and require HTTPS except on localhost, so a site reached by
IP address has no passkeys; it says so instead of failing in the browser.

**Challenges are stateless.** The random challenge travels inside a short-lived signed
token of its own type (``typ: webauthn``) and comes back with the browser's response.
A registration challenge is spent on use (its ``jti`` is revoked). A sign-in challenge is
tied to the two-factor step token it was issued for, and that token is single-use.

Verification itself (signatures, counters, origin and relying party checks) is done by
the ``webauthn`` library, not here.
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.config.settings import Settings
from app.core.errors import ConflictError, NotFoundError, TokenError, ValidationError
from app.core.logging import get_logger
from app.core.security import TokenService
from app.models.audit import AuditAction
from app.models.auth import User, UserPasskey
from app.repositories.auth_tokens import RevokedTokenRepository
from app.services.audit import AuditService

log = get_logger(__name__)

#: How long the browser has between asking for options and answering.
CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS = 10

#: Anything the library raises for a bad answer, plus malformed input it trips over.
_VERIFY_ERRORS = (WebAuthnException, ValueError, KeyError, TypeError)


@dataclass(frozen=True, slots=True)
class RelyingParty:
    rp_id: str
    origins: list[str]


@dataclass(frozen=True, slots=True)
class PasskeyOptions:
    """What the browser passes to ``navigator.credentials``, and the challenge token."""

    public_key: dict[str, Any]
    challenge_token: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def relying_party(settings: Settings) -> RelyingParty | None:
    """The relying party passkeys are bound to here, or None if this site cannot have one."""
    base = urlsplit(settings.platform.public_base_url)
    rp_id = (settings.auth.webauthn_rp_id or base.hostname or "").strip().lower()
    if not rp_id:
        return None
    try:
        ipaddress.ip_address(rp_id)
        return None  # browsers refuse an IP address as a relying party id
    except ValueError:
        pass
    origins = list(settings.auth.webauthn_origins)
    if not origins and base.scheme and base.netloc:
        origins = [f"{base.scheme}://{base.netloc}"]
    usable = [
        o.rstrip("/")
        for o in origins
        if o.startswith("https://") or urlsplit(o).hostname in ("localhost", "127.0.0.1")
    ]
    return RelyingParty(rp_id=rp_id, origins=usable) if usable else None


UNAVAILABLE = (
    "Passkeys need the platform to be reached by a host name over HTTPS (or on localhost). "
    "Set PLATFORM__PUBLIC_BASE_URL, or AUTH__WEBAUTHN_RP_ID and AUTH__WEBAUTHN_ORIGINS. "
    "Until then, use the authenticator app."
)


class PasskeyService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        tokens: TokenService,
        audit: AuditService,
        revocations: RevokedTokenRepository | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._tokens = tokens
        self._audit = audit
        self._revocations = revocations or RevokedTokenRepository(session)

    # -- availability --------------------------------------------------------------
    @property
    def relying_party(self) -> RelyingParty | None:
        return relying_party(self._settings)

    def _require_rp(self) -> RelyingParty:
        rp = self.relying_party
        if rp is None:
            raise ConflictError(UNAVAILABLE)
        return rp

    async def list_for(self, user: User) -> list[UserPasskey]:
        rows = await self._session.execute(
            select(UserPasskey)
            .where(UserPasskey.user_id == user.id)
            .order_by(UserPasskey.created_at)
        )
        return list(rows.scalars().all())

    async def count_for(self, user: User) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count()).where(UserPasskey.user_id == user.id)
                )
            ).scalar_one()
        )

    async def usable_for(self, user: User) -> bool:
        """Whether the two-factor step may be answered with a passkey for this account."""
        return (
            user.mfa_enabled and self.relying_party is not None and await self.count_for(user) > 0
        )

    # -- registration --------------------------------------------------------------
    async def registration_options(self, user: User) -> PasskeyOptions:
        """Options for adding a passkey. The caller has confirmed the password."""
        rp = self._require_rp()
        if user.auth_provider != "local":
            raise ValidationError(
                "Passkeys are for accounts that sign in with a platform password. Your "
                "identity provider owns your second factor."
            )
        if not user.mfa_enabled:
            raise ValidationError(
                "Turn on two-factor authentication with an authenticator app first. The app "
                "and your recovery codes stay as the way back in if you lose a passkey."
            )
        existing = await self.list_for(user)
        if len(existing) >= MAX_PASSKEYS:
            raise ConflictError(f"You already have {MAX_PASSKEYS} passkeys. Remove one first.")
        challenge = secrets.token_bytes(32)
        options = generate_registration_options(
            rp_id=rp.rp_id,
            rp_name=self._settings.auth.mfa_issuer,
            user_id=user.id.bytes,
            user_name=user.username,
            user_display_name=user.full_name or user.username,
            challenge=challenge,
            timeout=CHALLENGE_TTL_SECONDS * 1000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.DISCOURAGED,
                user_verification=UserVerificationRequirement.PREFERRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=p.credential_id) for p in existing
            ],
        )
        token = self._tokens.create_webauthn_token(
            str(user.id),
            CHALLENGE_TTL_SECONDS,
            purpose="register",
            challenge=_b64url(challenge),
            sv=user.session_version or 0,
        )
        return PasskeyOptions(json.loads(options_to_json(options)), token)

    async def register(
        self, user: User, *, challenge_token: str, credential: dict[str, Any], name: str
    ) -> UserPasskey:
        rp = self._require_rp()
        payload = await self._challenge(challenge_token, user, purpose="register")
        spent = await self._revocations.revoke(
            jti=str(payload["jti"]),
            user_id=user.id,
            token_type="webauthn",  # noqa: S106 — a token kind, not a credential
            reason="used",
            expires_at=dt.datetime.fromtimestamp(int(payload["exp"]), dt.UTC),
        )
        if not spent:  # the same challenge, answered a moment ago in another request
            raise TokenError("This passkey challenge was already used. Start again.")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=_unb64url(str(payload["challenge"])),
                expected_rp_id=rp.rp_id,
                expected_origin=rp.origins,
            )
        except _VERIFY_ERRORS as exc:
            log.info("passkey_registration_refused", user=str(user.id), error=str(exc))
            raise ValidationError(
                "The browser's answer could not be verified. Try adding the passkey again."
            ) from exc

        clash = await self._session.execute(
            select(UserPasskey.id).where(UserPasskey.credential_id == verified.credential_id)
        )
        if clash.first() is not None:
            raise ConflictError("That passkey is already registered.")

        transports = []
        raw = credential.get("response", {}).get("transports") or []
        for value in raw if isinstance(raw, list) else []:
            try:
                transports.append(str(AuthenticatorTransport(value).value))
            except ValueError:
                continue
        passkey = UserPasskey(
            user_id=user.id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            transports=transports,
            name=name.strip()[:64] or "Passkey",
            backed_up=bool(verified.credential_backed_up),
        )
        self._session.add(passkey)
        await self._session.flush()
        await self._audit.record(
            AuditAction.USER_PASSKEY_ADDED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={
                "passkey": str(passkey.id),
                "name": passkey.name,
                "synced": passkey.backed_up,
            },
        )
        return passkey

    async def remove(self, user: User, passkey_id: uuid.UUID) -> None:
        passkey = await self._session.get(UserPasskey, passkey_id)
        if passkey is None or passkey.user_id != user.id:
            raise NotFoundError("No such passkey on your account.")
        await self._session.delete(passkey)
        await self._audit.record(
            AuditAction.USER_PASSKEY_REMOVED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"passkey": str(passkey_id), "name": passkey.name},
        )

    # -- the two-factor step -------------------------------------------------------
    async def authentication_options(self, user: User, *, step_jti: str) -> PasskeyOptions:
        """Options for answering the two-factor step whose token has id ``step_jti``."""
        rp = self._require_rp()
        passkeys = await self.list_for(user)
        if not user.mfa_enabled or not passkeys:
            raise ValidationError("This account has no passkey. Enter a code instead.")
        challenge = secrets.token_bytes(32)
        options = generate_authentication_options(
            rp_id=rp.rp_id,
            challenge=challenge,
            timeout=CHALLENGE_TTL_SECONDS * 1000,
            allow_credentials=[
                PublicKeyCredentialDescriptor(
                    id=p.credential_id,
                    transports=[AuthenticatorTransport(t) for t in p.transports] or None,
                )
                for p in passkeys
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )
        token = self._tokens.create_webauthn_token(
            str(user.id),
            CHALLENGE_TTL_SECONDS,
            purpose="authenticate",
            challenge=_b64url(challenge),
            step=step_jti,
        )
        return PasskeyOptions(json.loads(options_to_json(options)), token)

    async def verify(
        self, user: User, *, challenge_token: str, credential: dict[str, Any], step_jti: str
    ) -> UserPasskey | None:
        """Check the browser's answer. None when it does not prove this account's passkey;
        the caller counts that like a wrong code."""
        rp = self._require_rp()
        payload = await self._challenge(challenge_token, user, purpose="authenticate")
        if payload.get("step") != step_jti:
            raise TokenError("This passkey challenge belongs to another sign-in. Start again.")
        try:
            credential_id = _unb64url(str(credential.get("rawId") or credential.get("id") or ""))
        except (ValueError, TypeError):
            return None
        passkey = (
            await self._session.execute(
                select(UserPasskey).where(
                    UserPasskey.credential_id == credential_id, UserPasskey.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if passkey is None:
            return None
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=_unb64url(str(payload["challenge"])),
                expected_rp_id=rp.rp_id,
                expected_origin=rp.origins,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
            )
        except _VERIFY_ERRORS as exc:
            # Includes a signature counter that went backwards: a cloned authenticator.
            log.warning("passkey_assertion_refused", user=str(user.id), error=str(exc))
            return None
        passkey.sign_count = verified.new_sign_count
        passkey.backed_up = bool(verified.credential_backed_up)
        passkey.last_used_at = dt.datetime.now(dt.UTC)
        return passkey

    # -- helpers -------------------------------------------------------------------
    async def _challenge(self, token: str, user: User, *, purpose: str) -> dict[str, Any]:
        payload = self._tokens.decode(token, expected_type="webauthn")
        if payload.get("purpose") != purpose or payload.get("sub") != str(user.id):
            raise TokenError("This passkey challenge is not for this request. Start again.")
        if "sv" in payload and int(payload["sv"]) != (user.session_version or 0):
            raise TokenError("This passkey challenge has expired. Start again.")
        jti = payload.get("jti")
        if not jti or await self._revocations.is_revoked(str(jti)):
            raise TokenError("This passkey challenge was already used. Start again.")
        return payload
