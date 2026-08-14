"""User administration service (M03)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import PasswordHasherService
from app.models.audit import AuditAction, AuditResult
from app.models.auth import Permission, Role, User
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository
from app.services.audit import AuditService

log = get_logger(__name__)

_MIN_PASSWORD_LENGTH = 12


class UserService:
    def __init__(
        self,
        users: UserRepository,
        roles: RoleRepository,
        permissions: PermissionRepository,
        hasher: PasswordHasherService,
        audit: AuditService,
    ) -> None:
        self._users = users
        self._roles = roles
        self._permissions = permissions
        self._hasher = hasher
        self._audit = audit

    async def list_users(self, *, limit: int = 50, offset: int = 0) -> Sequence[User]:
        return await self._users.list_with_roles(limit=limit, offset=offset)

    async def count_users(self) -> int:
        return await self._users.count()

    async def list_roles(self) -> Sequence[Role]:
        """Exposed through the service so routers never reach a repository (Rule 6)."""
        return await self._roles.list_all()

    async def list_permissions(self) -> Sequence[Permission]:
        return await self._permissions.list_all()

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self._users.get_with_roles(user_id)
        if user is None:
            raise NotFoundError(f"No user with id {user_id}.")
        return user

    async def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        full_name: str | None = None,
        role_names: Sequence[str] = (),
        actor: User | None = None,
    ) -> User:
        """Create a user and assign roles.

        Uniqueness is checked before insert so the caller gets a 409 naming the
        conflicting field rather than a raw integrity error. The database
        constraints remain the real guarantee against a concurrent duplicate.
        """
        self._validate_password(password)

        if await self._users.get_by_username(username):
            raise ConflictError(
                f"The username {username!r} is already taken.",
                details={"field": "username"},
            )
        if await self._users.get_by_email(email):
            raise ConflictError(
                f"The email {email!r} is already registered.",
                details={"field": "email"},
            )

        roles = []
        for name in role_names:
            role = await self._roles.get_by_name(name)
            if role is None:
                raise ValidationError(
                    f"No such role: {name!r}.", details={"field": "roles", "value": name}
                )
            roles.append(role)

        user = User(
            username=username,
            email=email,
            full_name=full_name,
            hashed_password=self._hasher.hash(password),
            is_active=True,
            is_superuser=False,
            roles=roles,
        )
        self._users.add(user)
        await self._users.flush()

        await self._audit.record(
            AuditAction.USER_CREATED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else "system",
            resource_type="user",
            resource_id=str(user.id),
            metadata={"username": username, "roles": list(role_names)},
        )
        return user

    async def set_active(self, user_id: uuid.UUID, *, active: bool, actor: User) -> User:
        user = await self.get_user(user_id)
        if user.id == actor.id and not active:
            # Cheap guard against an admin locking themselves out of the platform.
            raise ValidationError("You cannot disable your own account.")
        user.is_active = active
        await self._audit.record(
            AuditAction.USER_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"is_active": active},
        )
        return user

    async def set_password(self, user_id: uuid.UUID, password: str, *, actor: User) -> User:
        """An administrator setting someone's password.

        Refused for a federated account. Setting one would create exactly the bypass the
        federation rules exist to prevent: a password that still signs the account in
        after the directory has removed the person.
        """
        user = await self.get_user(user_id)
        if user.auth_provider != "local":
            raise ValidationError(
                f"{user.username!r} signs in through the {user.auth_provider!r} provider. "
                "Giving it a platform password would let it keep signing in after the "
                "directory removes the person. Disable the account here instead.",
                details={"field": "password"},
            )
        self._validate_password(password)
        user.hashed_password = self._hasher.hash(password)
        # Reset so an administrator's help does not leave the account near a lockout
        # threshold from the failures that prompted the call.
        user.failed_login_count = 0

        await self._audit.record(
            AuditAction.USER_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="user",
            resource_id=str(user.id),
            # The password itself is never recorded — but the fact that someone else set
            # it is exactly what an audit needs to show.
            metadata={"password_set_by_admin": True, "subject": user.username},
        )
        return user

    async def change_own_password(
        self, user: User, *, current_password: str, new_password: str
    ) -> User:
        """Someone changing their own password.

        Verifying the current one is not redundant with holding a valid token: a token
        left on a shared workstation would otherwise be enough to take the account
        permanently, which is a much worse outcome than reading a page.
        """
        if user.auth_provider != "local" or user.hashed_password is None:
            raise ValidationError(
                "This account signs in through an external provider. Change your password "
                f"with {user.auth_provider!r}, not here."
            )
        if not self._hasher.verify(user.hashed_password, current_password):
            await self._audit.record_independent(
                AuditAction.USER_UPDATED,
                result=AuditResult.FAILURE,
                user_id=user.id,
                username=user.username,
                message="Password change refused: current password incorrect",
            )
            raise ValidationError(
                "That is not your current password.", details={"field": "current_password"}
            )
        if current_password == new_password:
            raise ValidationError(
                "The new password is the same as the current one.",
                details={"field": "new_password"},
            )
        self._validate_password(new_password)
        user.hashed_password = self._hasher.hash(new_password)

        await self._audit.record(
            AuditAction.USER_UPDATED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"password_changed_by_self": True},
        )
        return user

    async def assign_roles(
        self, user_id: uuid.UUID, role_names: Sequence[str], *, actor: User
    ) -> User:
        user = await self.get_user(user_id)
        roles = []
        for name in role_names:
            role = await self._roles.get_by_name(name)
            if role is None:
                raise ValidationError(f"No such role: {name!r}.")
            roles.append(role)
        user.roles = roles

        if user.auth_provider != "local":
            # Allowed, but recorded — with FEDERATION__SYNC_ROLES_ON_LOGIN on (the
            # default) the directory overwrites this at the person's next sign-in. An
            # administrator who grants access here and sees it vanish an hour later has
            # no way to tell why unless the platform says so.
            log.warning(
                "roles_assigned_to_federated_account",
                username=user.username,
                provider=user.auth_provider,
                detail=(
                    "Roles for a federated account are re-derived from directory groups "
                    "on the next sign-in unless FEDERATION__SYNC_ROLES_ON_LOGIN is off."
                ),
            )

        await self._audit.record(
            AuditAction.ROLE_ASSIGNED,
            user_id=actor.id,
            username=actor.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"roles": list(role_names)},
        )
        return user

    @staticmethod
    def _validate_password(password: str) -> None:
        """Minimum length only.

        Length is the property that actually correlates with resistance to
        guessing; composition rules mostly push people toward predictable
        substitutions. Phase 6 can add a breach-list check, which works offline
        from a bundled list.
        """
        if len(password) < _MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.",
                details={"field": "password"},
            )
