"""Turning an external identity into a platform account (M03, Phase 6).

This module is the trust boundary. A provider says "this is Fatima, and the directory puts
her in these groups"; this decides whether the platform has an account for her and what she
is allowed to do. Those are different questions, and merging them is the mistake that makes
an Active Directory group name a privilege grant.

Four rules, each closing a way an external directory could otherwise take over the
platform:

1. **Match on the provider's subject, not the username.** Usernames are reassigned when
   people leave. Matching on one hands a new joiner the previous holder's roles, history
   and audit trail — under the previous holder's name.
2. **Never federate over a local account.** If ``admin`` exists locally, a directory user
   called ``admin`` gets a refusal, not that account. Otherwise anyone who can create a
   directory entry can take the break-glass account.
3. **A mapping can never grant SUPER_ADMIN.** That role bypasses permission checks
   entirely, so it stays something an existing administrator confers deliberately, inside
   the platform.
4. **Group changes apply on the next sign-in.** Removing someone from a group in the
   directory removes their platform access, because otherwise the directory stops being
   authoritative the moment it is first read.
"""

from __future__ import annotations

from app.config.settings import FederationSettings
from app.core.errors import AuthenticationError
from app.core.interfaces.auth_provider import ExternalIdentity
from app.core.logging import get_logger
from app.core.permissions import Role as RoleName
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.repositories.user import RoleRepository, UserRepository
from app.services.audit import AuditService

log = get_logger(__name__)


class FederationService:
    def __init__(
        self,
        users: UserRepository,
        roles: RoleRepository,
        audit: AuditService,
        settings: FederationSettings,
    ) -> None:
        self._users = users
        self._roles = roles
        self._audit = audit
        self._settings = settings

    async def resolve(self, identity: ExternalIdentity) -> User:
        """Find or create the platform account for an authenticated external identity."""
        user = await self._find(identity)

        if user is None:
            if not self._settings.auto_provision:
                await self._audit.record_independent(
                    AuditAction.USER_LOGIN,
                    result=AuditResult.DENIED,
                    username=identity.username,
                    message=f"No account, and auto-provisioning is off ({identity.provider})",
                )
                raise AuthenticationError(
                    "You authenticated successfully, but this platform has no account for "
                    "you and does not create them automatically. Ask an administrator."
                )
            user = await self._provision(identity)
        else:
            await self._reconcile(user, identity)

        if not user.is_active:
            # Checked here as well as at the provider: disabling someone in the platform
            # must lock them out even while the directory still happily authenticates them.
            await self._audit.record_independent(
                AuditAction.USER_LOGIN,
                result=AuditResult.DENIED,
                user_id=user.id,
                username=user.username,
                message="Account is disabled in the platform",
            )
            raise AuthenticationError("This account is disabled.")

        return user

    # -- lookup ------------------------------------------------------------
    async def _find(self, identity: ExternalIdentity) -> User | None:
        existing = await self._users.get_by_external_subject(identity.provider, identity.subject)
        if existing is not None:
            return existing

        # No subject match. A username match now is either an account created by an
        # administrator ahead of first sign-in, or a collision — and those need opposite
        # outcomes, so the provider on the row decides.
        by_username = await self._users.get_by_username(identity.username)
        if by_username is None:
            return None

        if by_username.auth_provider == "local":
            log.warning(
                "federation_local_collision",
                username=identity.username,
                provider=identity.provider,
            )
            raise AuthenticationError(
                f"A local platform account already uses the name {identity.username!r}. "
                "Sign in with its password, or ask an administrator to rename one of them. "
                "The platform will not attach a directory identity to a local account."
            )

        if by_username.auth_provider != identity.provider:
            raise AuthenticationError(
                f"The account {identity.username!r} belongs to the "
                f"{by_username.auth_provider!r} provider, not {identity.provider!r}."
            )

        if (
            by_username.external_subject is not None
            and by_username.external_subject != identity.subject
        ):
            # The leaver/joiner collision, and the whole reason Rule 1 exists. This
            # account already belongs to a *different* directory subject that happened to
            # hold the same username. Claiming it here would hand the new person the
            # previous holder's roles and audit history under the previous holder's name —
            # the exact outcome matching on subject was meant to prevent.
            #
            # Refused rather than auto-renamed: usernames are unique platform-wide, so
            # resolving this needs a decision about which name each person keeps, and that
            # is an administrator's call.
            log.warning(
                "federation_username_reused",
                username=identity.username,
                provider=identity.provider,
            )
            raise AuthenticationError(
                f"The username {identity.username!r} already belongs to a different "
                "directory account on this platform. An administrator needs to rename or "
                "remove the old one — the platform will not transfer an existing account "
                "to a new directory identity."
            )

        # Same provider, no subject recorded: an account pre-created by an administrator,
        # or one from before subjects were stored. Claim it and record the subject so this
        # path is never taken again for this person.
        by_username.external_subject = identity.subject
        return by_username

    async def _provision(self, identity: ExternalIdentity) -> User:
        if await self._users.get_by_email(identity.email):
            raise AuthenticationError(
                f"Another platform account already uses the address {identity.email!r}."
            )

        user = User(
            username=identity.username,
            email=identity.email,
            full_name=identity.full_name,
            # No password, ever. The login path refuses a null hash, so this account
            # cannot be signed into except through its provider.
            hashed_password=None,
            auth_provider=identity.provider,
            external_subject=identity.subject,
            is_active=True,
            is_superuser=False,
            roles=await self._roles_for(identity),
        )
        self._users.add(user)
        await self._users.flush()

        await self._audit.record(
            AuditAction.USER_CREATED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={
                "provider": identity.provider,
                "groups": list(identity.groups),
                "roles": [r.name for r in user.roles],
                "auto_provisioned": True,
            },
        )
        log.info(
            "federated_user_provisioned",
            username=user.username,
            provider=identity.provider,
            roles=[r.name for r in user.roles],
        )
        return user

    async def _reconcile(self, user: User, identity: ExternalIdentity) -> None:
        """Bring an existing account back into step with what the directory now says."""
        changed: dict[str, object] = {}

        if (
            identity.email
            and user.email != identity.email
            # Skipped rather than raised: someone else already holds the new address, and
            # failing the sign-in over a stale email would lock out a person whose
            # credential was perfectly good.
            and await self._users.get_by_email(identity.email) is None
        ):
            changed["email"] = identity.email
            user.email = identity.email
        if identity.full_name and user.full_name != identity.full_name:
            changed["full_name"] = identity.full_name
            user.full_name = identity.full_name

        if self._settings.sync_roles_on_login:
            desired = await self._roles_for(identity)
            before = {r.name for r in user.roles}
            after = {r.name for r in desired}
            if before != after:
                # A superuser's roles are still synced, but is_superuser is untouched —
                # it is not a role and no directory group can confer it.
                changed["roles"] = sorted(after)
                changed["roles_before"] = sorted(before)
                user.roles = desired

        if changed:
            await self._audit.record(
                AuditAction.USER_UPDATED,
                user_id=user.id,
                username=user.username,
                resource_type="user",
                resource_id=str(user.id),
                metadata={"provider": identity.provider, "synced": changed},
            )

    # -- role mapping ------------------------------------------------------
    async def _roles_for(self, identity: ExternalIdentity) -> list:
        mapping = {k.casefold(): v for k, v in self._settings.role_mapping.items()}
        wanted: list[str] = []
        for group in identity.groups:
            role_name = mapping.get(group.casefold())
            if role_name and role_name not in wanted:
                wanted.append(role_name)

        # Only when NO group matched. A mapping that matched but resolved to nothing —
        # refused SUPER_ADMIN, or a role that does not exist — grants nothing, rather than
        # quietly substituting a role the operator never wrote. The person signs in and
        # can do nothing, which is safe and visible; the alternative is a silent grant.
        if not wanted:
            wanted = list(self._settings.default_roles)

        resolved = []
        for name in wanted:
            if name == RoleName.SUPER_ADMIN:
                # Rule 3. Logged loudly rather than silently dropped: an operator who wrote
                # this mapping believes it works, and will otherwise wonder why it doesn't.
                log.warning(
                    "federation_refused_superadmin",
                    provider=identity.provider,
                    username=identity.username,
                    detail=(
                        "A group maps to SUPER_ADMIN. Refused — that role bypasses every "
                        "permission check and cannot be granted by a directory."
                    ),
                )
                continue
            role = await self._roles.get_by_name(name)
            if role is None:
                log.warning(
                    "federation_unknown_role",
                    role=name,
                    detail="Named in FEDERATION__ROLE_MAPPING but no such platform role.",
                )
                continue
            resolved.append(role)
        return resolved
