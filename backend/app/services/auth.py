"""Authentication and authorisation service (M03).

Local username/password, plus any provider configured behind the §28
:class:`~app.core.interfaces.auth_provider.AuthProvider` boundary. The platform's own
JWTs remain the internal representation whoever vouched for the person, so nothing
downstream of this service — permissions, audit, the gateway — knows or cares which
provider was involved.

Authorisation lives here rather than in the routers so a worker or CLI can apply
the same rules with no request in scope.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import AuthenticationError, PermissionDeniedError, TokenError
from app.core.interfaces.auth_provider import ExternalIdentity, PasswordAuthProvider
from app.core.logging import get_logger
from app.core.security import PasswordHasherService, TokenService
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.repositories.user import UserRepository
from app.services.audit import AuditService
from app.services.federation import FederationService

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — an OAuth2 type name, not a credential
    expires_in: int = 3600


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        hasher: PasswordHasherService,
        tokens: TokenService,
        audit: AuditService,
        password_providers: Sequence[PasswordAuthProvider] = (),
        federation: FederationService | None = None,
    ) -> None:
        self._users = users
        self._hasher = hasher
        self._tokens = tokens
        self._audit = audit
        # External password providers only — local is handled inline below, because it
        # also owns lockout counting and hash upgrades, which no other provider has.
        self._password_providers = list(password_providers)
        self._federation = federation

    # -- authentication ----------------------------------------------------
    async def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str | None = None,
    ) -> TokenPair:
        """Verify credentials and issue tokens.

        Every failure returns the *same* error regardless of cause. Distinguishing
        "no such user" from "wrong password" would turn this endpoint into a user
        enumeration oracle.
        """
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
            raise AuthenticationError("Incorrect username or password.")

        if not self._hasher.verify(user.hashed_password, password):
            user.failed_login_count += 1
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                message="Incorrect password",
                metadata={"failed_login_count": user.failed_login_count},
                user_agent=user_agent,
            )
            raise AuthenticationError("Incorrect username or password.")

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

        user.last_login_at = dt.datetime.now(dt.UTC)
        user.failed_login_count = 0

        await self._audit.record(
            AuditAction.USER_LOGIN,
            user_id=user.id,
            username=user.username,
            user_agent=user_agent,
        )
        return self._issue(user)

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
        """Exchange a refresh token for a new pair."""
        payload = self._tokens.decode(refresh_token, expected_type="refresh")
        user = await self._resolve_subject(payload.get("sub"))

        if not user.is_active:
            raise AuthenticationError("This account is disabled.")

        await self._audit.record(
            AuditAction.USER_TOKEN_REFRESHED,
            user_id=user.id,
            username=user.username,
        )
        return self._issue(user)

    async def logout(self, user: User) -> None:
        """Record a logout.

        Stateless JWTs cannot be invalidated server-side without a revocation
        store; Phase 6 adds one keyed on the ``jti`` claim that
        :class:`TokenService` already issues. Until then this is an audit event and
        the client discards its tokens.
        """
        await self._audit.record(AuditAction.USER_LOGOUT, user_id=user.id, username=user.username)

    async def resolve_access_token(self, token: str) -> User:
        """Validate an access token and load the user it names."""
        payload = self._tokens.decode(token, expected_type="access")
        user = await self._resolve_subject(payload.get("sub"))
        if not user.is_active:
            raise AuthenticationError("This account is disabled.")
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
        return TokenPair(
            access_token=self._tokens.create_access_token(str(user.id), **claims),
            refresh_token=self._tokens.create_refresh_token(str(user.id)),
            expires_in=self._tokens.access_ttl_seconds,
        )

    async def _resolve_subject(self, subject: str | None) -> User:
        if not subject:
            raise TokenError()
        try:
            import uuid

            user_id = uuid.UUID(subject)
        except ValueError as exc:
            raise TokenError("Token subject is not a valid user id.") from exc

        user = await self._users.get_with_roles(user_id)
        if user is None:
            # Token verified but the account is gone — treat as unauthenticated.
            raise TokenError("The user this token refers to no longer exists.")
        return user
