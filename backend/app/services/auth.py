"""Authentication and authorisation service (M03, E14).

Local username/password, plus any provider configured behind the §28
:class:`~app.core.interfaces.auth_provider.AuthProvider` boundary. The platform's own
JWTs remain the internal representation whoever vouched for the person, so nothing
downstream of this service — permissions, audit, the gateway — knows or cares which
provider was involved.

Authorisation lives here rather than in the routers so a worker or CLI can apply
the same rules with no request in scope.

**Sessions (E14).** A token is refused before it expires when:

* its ``jti`` is in ``revoked_tokens``: the holder signed out, or it is a refresh token
  that has already been exchanged (refresh tokens are single-use);
* its ``sv`` claim is older than the account's ``session_version``: the password changed,
  the person chose "sign out everywhere", an administrator revoked the sessions, or a
  refresh token was reused, which is how a stolen one shows itself.

Tokens issued before E14 carry no ``sv`` and count as version 0, which every existing
account has, so an upgrade signs nobody out.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.core.errors import AuthenticationError, PermissionDeniedError, RateLimitError, TokenError
from app.core.interfaces.auth_provider import ExternalIdentity, PasswordAuthProvider
from app.core.logging import get_logger
from app.core.request_context import get_client_ip
from app.core.security import PasswordHasherService, TokenService
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.repositories.auth_tokens import RevokedTokenRepository
from app.repositories.user import UserRepository
from app.services.audit import AuditService
from app.services.federation import FederationService
from app.services.login_guard import LoginGuard, account_locked_fingerprint

log = get_logger(__name__)

_BAD_CREDENTIALS = "Incorrect username or password."
_SIGNED_OUT = "This session has been signed out. Sign in again."


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — an OAuth2 type name, not a credential
    expires_in: int = 3600


def _expiry(payload: dict[str, Any]) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(payload["exp"]), dt.UTC)


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        hasher: PasswordHasherService,
        tokens: TokenService,
        audit: AuditService,
        password_providers: Sequence[PasswordAuthProvider] = (),
        federation: FederationService | None = None,
        *,
        revocations: RevokedTokenRepository | None = None,
        guard: LoginGuard | None = None,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._tokens = tokens
        self._audit = audit
        # External password providers only — local is handled inline below, because it
        # also owns lockout counting and hash upgrades, which no other provider has.
        self._password_providers = list(password_providers)
        self._federation = federation
        self._revocations = revocations or RevokedTokenRepository(users.session)
        self._guard = guard

    # -- authentication ----------------------------------------------------
    async def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str | None = None,
    ) -> TokenPair:
        """Verify credentials and issue tokens.

        Every failure returns the *same* error regardless of cause, including a locked
        account. Distinguishing "no such user", "wrong password" or "locked" would turn
        this endpoint into a user enumeration oracle.
        """
        address = get_client_ip() or "unknown"
        if self._guard is not None:
            retry = await self._guard.check_address(address)
            if retry:
                await self._audit.record_independent(
                    AuditAction.LOGIN_THROTTLED,
                    result=AuditResult.DENIED,
                    username=username,
                    message="Sign-in refused: too many failures from this address",
                    metadata={"address": address},
                    user_agent=user_agent,
                )
                minutes = max(1, round(retry / 60))
                raise RateLimitError(
                    f"Too many failed sign-ins from this address. Try again in {minutes} "
                    f"minute{'s' if minutes != 1 else ''}.",
                    details={"retry_after_seconds": retry},
                )

        user = await self._users.get_by_username(username)

        if user is None or user.hashed_password is None or user.auth_provider != "local":
            # Not a local account. Offer the credential to each external password
            # provider before giving up — this is how a directory user signs in without
            # the platform ever having a row for them beforehand.
            federated = await self._try_external(username, password, user_agent=user_agent)
            if federated is not None:
                return federated

            # Hash anyway so a missing account and a wrong password take
            # comparable time; skipping the hash leaks account existence via
            # response latency.
            self._hasher.hash(password)
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.FAILURE,
                username=username,
                message="Unknown user, or no provider accepted the credential",
                user_agent=user_agent,
            )
            await self._address_failed(address)
            raise AuthenticationError(_BAD_CREDENTIALS)

        now = dt.datetime.now(dt.UTC)
        if user.locked_until is not None and user.locked_until > now:
            # The password is not checked while locked: otherwise the lock would still
            # tell a guesser when a guess was right. Hash anyway for comparable timing.
            self._hasher.hash(password)
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.DENIED,
                user_id=user.id,
                username=user.username,
                message="Account is locked",
                metadata={"locked_until": user.locked_until.isoformat()},
                user_agent=user_agent,
            )
            await self._address_failed(address)
            raise AuthenticationError(_BAD_CREDENTIALS)

        if not self._hasher.verify(user.hashed_password, password):
            failures, locked = (
                await self._guard.account_failed(user, now=now)
                if self._guard is not None
                else (user.failed_login_count + 1, False)
            )
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                message="Incorrect password",
                metadata={"failed_login_count": failures},
                user_agent=user_agent,
            )
            if locked and self._guard is not None:
                await self._guard.account_locked(user, audit=self._audit, failures=failures)
            await self._address_failed(address)
            raise AuthenticationError(_BAD_CREDENTIALS)

        if not user.is_active:
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.DENIED,
                user_id=user.id,
                username=user.username,
                message="Account is disabled",
                user_agent=user_agent,
            )
            # Distinct from bad credentials: the caller proved who they are, so
            # telling them the account is disabled leaks nothing they do not know.
            raise AuthenticationError("This account is disabled.")

        # Transparently upgrade a hash stored under weaker parameters.
        if self._hasher.needs_rehash(user.hashed_password):
            user.hashed_password = self._hasher.hash(password)
            log.info("password_hash_upgraded", username=user.username)

        was_locked = user.locked_until is not None
        user.last_login_at = now
        user.failed_login_count = 0
        user.locked_until = None
        if was_locked and self._guard is not None:
            await self._guard.clear_alert(account_locked_fingerprint(user.id))

        await self._audit.record(
            AuditAction.USER_LOGIN,
            user_id=user.id,
            username=user.username,
            user_agent=user_agent,
        )
        return self._issue(user)

    async def _address_failed(self, address: str) -> None:
        if self._guard is not None:
            await self._guard.address_failed(address, audit=self._audit)

    async def _try_external(
        self, username: str, password: str, *, user_agent: str | None
    ) -> TokenPair | None:
        """Offer a credential to each external password provider in turn.

        A provider returning ``None`` means "not my user" and the next is tried. A
        provider *raising* means the provider itself failed, and that propagates — because
        silently continuing would report a directory outage as a wrong password, sending
        the person to reset a password that was never the problem.
        """
        for provider in self._password_providers:
            identity = await provider.authenticate(username, password)
            if identity is None:
                continue
            if self._federation is None:  # pragma: no cover — wiring error, not a state
                raise AuthenticationError("No federation service is configured.")

            user = await self._federation.resolve(identity)
            user.last_login_at = dt.datetime.now(dt.UTC)
            user.failed_login_count = 0
            await self._audit.record(
                AuditAction.USER_LOGIN,
                user_id=user.id,
                username=user.username,
                user_agent=user_agent,
                metadata={"provider": provider.name},
            )
            return self._issue(user)
        return None

    async def login_external(
        self, identity: ExternalIdentity, *, user_agent: str | None = None
    ) -> TokenPair:
        """Issue tokens for an identity a redirect provider has already verified.

        Separate from :meth:`login` because there is no credential to check here — the
        provider did that. The account resolution and role mapping are identical, which is
        the point of routing both through :class:`FederationService`.
        """
        if self._federation is None:  # pragma: no cover — wiring error
            raise AuthenticationError("No federation service is configured.")
        user = await self._federation.resolve(identity)
        user.last_login_at = dt.datetime.now(dt.UTC)
        user.failed_login_count = 0
        await self._audit.record(
            AuditAction.USER_LOGIN,
            user_id=user.id,
            username=user.username,
            user_agent=user_agent,
            metadata={"provider": identity.provider},
        )
        return self._issue(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        """Exchange a refresh token for a new pair. Each refresh token works once.

        A refresh token presented again shortly after it was exchanged is refused
        quietly: two browser tabs refreshing at once. Presented again later, it has
        been copied, so every session for the account is signed out and an alert raised.
        """
        payload = self._tokens.decode(refresh_token, expected_type="refresh")
        user = await self._resolve_subject(payload.get("sub"))

        if not user.is_active:
            raise AuthenticationError("This account is disabled.")
        if int(payload.get("sv", 0)) != user.session_version:
            raise TokenError(_SIGNED_OUT)
        jti = payload.get("jti")
        if not jti:
            raise TokenError()

        exchanged = await self._revocations.revoke(
            jti=str(jti),
            user_id=user.id,
            token_type="refresh",  # noqa: S106 — a token kind, not a credential
            reason="rotated",
            expires_at=_expiry(payload),
        )
        if not exchanged:
            await self._refused_refresh(user, str(jti))

        await self._audit.record(
            AuditAction.USER_TOKEN_REFRESHED,
            user_id=user.id,
            username=user.username,
        )
        return self._issue(user)

    async def _refused_refresh(self, user: User, jti: str) -> None:
        previous = await self._revocations.get(jti)
        grace = self._guard.settings.refresh_reuse_grace_seconds if self._guard else 0
        if previous is not None and previous.reason == "rotated":
            age = (dt.datetime.now(dt.UTC) - previous.revoked_at).total_seconds()
            if age > grace and self._guard is not None:
                await self._guard.sign_out_everywhere(
                    user,
                    audit=self._audit,
                    reason=f"Refresh token reused {int(age)} s after it was exchanged",
                )
                raise TokenError(
                    "This refresh token was already used, so every session for this account "
                    "has been signed out. Sign in again."
                )
        raise TokenError("This refresh token is no longer valid.")

    async def logout(
        self, user: User, *, access_token: str | None = None, refresh_token: str | None = None
    ) -> None:
        """Sign out this session: its access token, and its refresh token if given."""
        revoked = []
        for token, kind in ((access_token, "access"), (refresh_token, "refresh")):
            if not token:
                continue
            try:
                payload = self._tokens.decode(token, expected_type=kind)  # type: ignore[arg-type]
            except TokenError:
                continue  # already expired or not this kind: nothing to revoke
            if payload.get("sub") != str(user.id) or not payload.get("jti"):
                continue  # never revoke somebody else's token
            await self._revocations.revoke(
                jti=str(payload["jti"]),
                user_id=user.id,
                token_type=kind,
                reason="logout",
                expires_at=_expiry(payload),
            )
            revoked.append(kind)
        await self._audit.record(
            AuditAction.USER_LOGOUT,
            user_id=user.id,
            username=user.username,
            metadata={"revoked": revoked},
        )

    async def logout_everywhere(self, user: User) -> None:
        """Sign out every session of this account, including the current one."""
        user.session_version += 1
        await self._audit.record(
            AuditAction.USER_SESSIONS_REVOKED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"by": "self"},
        )

    async def resolve_access_token(self, token: str) -> User:
        """Validate an access token and load the user it names."""
        payload = self._tokens.decode(token, expected_type="access")
        jti = payload.get("jti")
        if jti and await self._revocations.is_revoked(str(jti)):
            raise TokenError(_SIGNED_OUT)
        user = await self._resolve_subject(payload.get("sub"))
        if not user.is_active:
            raise AuthenticationError("This account is disabled.")
        if int(payload.get("sv", 0)) != user.session_version:
            raise TokenError(_SIGNED_OUT)
        return user

    # -- authorisation -----------------------------------------------------
    @staticmethod
    def has_permission(user: User, permission: str) -> bool:
        if user.is_superuser:
            return True
        return permission in user.effective_permissions

    async def require_permission(self, user: User, permission: str) -> None:
        """Raise :class:`PermissionDeniedError` unless the user holds ``permission``.

        The refusal is audited before raising, and independently of the request
        transaction, so denials are never lost to the rollback.
        """
        if self.has_permission(user, permission):
            return
        await self._audit.record_denied(
            "PERMISSION_CHECK_FAILED",
            user_id=user.id,
            username=user.username,
            required_permission=permission,
        )
        raise PermissionDeniedError(
            f"This action requires the {permission!r} permission.",
            details={"required_permission": permission},
        )

    # -- internals ---------------------------------------------------------
    def _issue(self, user: User) -> TokenPair:
        # Roles are embedded for display convenience only. Authorisation always
        # re-reads permissions from the database, so revoking a role takes effect
        # on the next request rather than when the token happens to expire.
        claims = {"username": user.username, "roles": [role.name for role in user.roles]}
        version = user.session_version or 0
        return TokenPair(
            access_token=self._tokens.create_access_token(str(user.id), sv=version, **claims),
            refresh_token=self._tokens.create_refresh_token(str(user.id), sv=version),
            expires_in=self._tokens.access_ttl_seconds,
        )

    async def _resolve_subject(self, subject: str | None) -> User:
        if not subject:
            raise TokenError()
        try:
            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise TokenError("Token subject is not a valid user id.") from exc

        user = await self._users.get_with_roles(user_id)
        if user is None:
            # Token verified but the account is gone — treat as unauthenticated.
            raise TokenError("The user this token refers to no longer exists.")
        return user
