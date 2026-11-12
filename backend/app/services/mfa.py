"""Two-factor authentication with an authenticator app (expansion E15).

Local accounts only. A directory or single sign-on account authenticates at its provider,
and that provider owns its second factor; asking for another here would be a second,
unrelated enrolment for the same person.

**Lifecycle.**

1. :meth:`MfaService.begin` stores a new secret, encrypted, and returns it with the
   ``otpauth://`` URI and a QR code. Nothing changes at sign-in yet.
2. :meth:`MfaService.activate` checks a first code against it. Only then is MFA on, and
   ten single-use recovery codes are issued. They are shown once.
3. :meth:`MfaService.check` verifies a code at sign-in, or for a self-service change.

**Consuming a code is a conditional UPDATE**, not a read-then-write. Two requests carrying
the same code at the same moment would otherwise both read the old last step and both
succeed. The row lock makes the second wait for the first, then match no row.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import AuthSettings
from app.core import totp
from app.core.errors import ConflictError, ValidationError
from app.core.permissions import Role as SystemRole
from app.core.security import SecretCipher
from app.models.audit import AuditAction
from app.models.auth import User
from app.services.audit import AuditService

#: Roles whose holders count as administrators for ``auth.mfa_required = admins``.
ADMIN_ROLES = frozenset({SystemRole.SUPER_ADMIN, SystemRole.ADMIN})

LOCAL_ONLY = (
    "Two-factor authentication is set up at your identity provider, not here, because "
    "you sign in through it."
)


@dataclass(frozen=True, slots=True)
class MfaSetup:
    secret: str
    otpauth_uri: str
    qr_svg: str


def is_admin(user: User) -> bool:
    return user.is_superuser or any(role.name in ADMIN_ROLES for role in user.roles)


def mfa_required_for(user: User, policy: str) -> bool:
    """Whether ``policy`` (``auth.mfa_required``) obliges this account to use MFA."""
    if user.auth_provider != "local":
        return False
    if policy == "all":
        return True
    return policy == "admins" and is_admin(user)


class MfaService:
    def __init__(
        self,
        session: AsyncSession,
        cipher: SecretCipher,
        audit: AuditService,
        settings: AuthSettings,
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._audit = audit
        self._settings = settings

    # -- enrolment ---------------------------------------------------------------------
    def begin(self, user: User) -> MfaSetup:
        """Store a fresh secret for ``user`` and return what the authenticator app needs."""
        if user.auth_provider != "local":
            raise ValidationError(LOCAL_ONLY)
        if user.mfa_enabled:
            raise ConflictError(
                "Two-factor authentication is already on. Turn it off first to move it to "
                "another device."
            )
        secret = totp.new_secret()
        user.mfa_secret_encrypted = self._cipher.encrypt(secret)
        user.mfa_last_step = None
        uri = totp.provisioning_uri(secret, account=user.username, issuer=self._settings.mfa_issuer)
        return MfaSetup(secret=secret, otpauth_uri=uri, qr_svg=totp.qr_svg(uri))

    async def activate(self, user: User, code: str) -> list[str]:
        """Turn MFA on once ``code`` proves the app holds the secret. Returns recovery codes."""
        if user.mfa_enabled:
            raise ConflictError("Two-factor authentication is already on.")
        if not user.mfa_secret_encrypted:
            raise ValidationError("Start the set-up first: there is no code to check against.")
        step = totp.verify(self._cipher.decrypt(user.mfa_secret_encrypted), code)
        if step is None:
            raise ValidationError(
                "That code is not right. Check the time on your phone, then enter the code "
                "shown now."
            )
        codes = totp.new_recovery_codes()
        user.mfa_enabled_at = dt.datetime.now(dt.UTC)
        user.mfa_last_step = step
        user.mfa_recovery_hashes = [totp.hash_recovery_code(c) for c in codes]
        await self._audit.record(
            AuditAction.USER_MFA_ENABLED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
        )
        return codes

    # -- verification ------------------------------------------------------------------
    async def check(self, user: User, code: str) -> str | None:
        """Consume ``code``. Returns ``"totp"`` or ``"recovery_code"``, or None if wrong."""
        if not user.mfa_enabled or not user.mfa_secret_encrypted:
            return None
        secret = self._cipher.decrypt(user.mfa_secret_encrypted)
        step = totp.verify(secret, code, last_step=user.mfa_last_step)
        if step is not None:
            consumed = await self._session.execute(
                update(User)
                .where(
                    User.id == user.id,
                    or_(User.mfa_last_step.is_(None), User.mfa_last_step < step),
                )
                .values(mfa_last_step=step)
                .returning(User.id)
                .execution_options(synchronize_session=False)
            )
            if consumed.first() is None:
                return None  # the same code, accepted a moment ago in another request
            await self._session.refresh(user, ["mfa_last_step"])
            return "totp"

        if not totp.looks_like_recovery_code(code):
            return None
        hashed = totp.hash_recovery_code(code)
        consumed = await self._session.execute(
            update(User)
            .where(User.id == user.id, User.mfa_recovery_hashes.has_key(hashed))
            .values(mfa_recovery_hashes=User.mfa_recovery_hashes.op("-")(hashed))
            .returning(User.id)
            .execution_options(synchronize_session=False)
        )
        if consumed.first() is None:
            return None
        await self._session.refresh(user, ["mfa_recovery_hashes"])
        await self._audit.record(
            AuditAction.USER_MFA_RECOVERY_CODE_USED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"remaining": len(user.mfa_recovery_hashes)},
        )
        return "recovery_code"

    # -- changes -----------------------------------------------------------------------
    async def regenerate_recovery_codes(self, user: User) -> list[str]:
        codes = totp.new_recovery_codes()
        user.mfa_recovery_hashes = [totp.hash_recovery_code(c) for c in codes]
        await self._audit.record(
            AuditAction.USER_MFA_RECOVERY_CODES_REGENERATED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
        )
        return codes

    @staticmethod
    def _clear(user: User) -> None:
        user.mfa_secret_encrypted = None
        user.mfa_enabled_at = None
        user.mfa_last_step = None
        user.mfa_recovery_hashes = []

    async def disable(self, user: User) -> None:
        """The account holder turns MFA off (the caller has checked password and code)."""
        self._clear(user)
        await self._audit.record(
            AuditAction.USER_MFA_DISABLED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
        )

    async def reset(self, user: User, *, actor: User) -> None:
        """An administrator removes MFA from an account whose holder lost the device.

        Also signs the account out everywhere: whoever asked for the reset might not be
        the account holder, and the holder should notice the next time they sign in.
        """
        if user.auth_provider != "local":
            raise ValidationError(LOCAL_ONLY)
        was_enabled = user.mfa_enabled
        self._clear(user)
        user.session_version += 1
        await self._audit.record(
            AuditAction.USER_MFA_RESET,
            user_id=actor.id,
            username=actor.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"subject": user.username, "was_enabled": was_enabled},
        )
