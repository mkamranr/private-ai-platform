"""Idempotent bootstrap seeding (M03).

Seeds the §M03 permission catalogue, the eight system roles and — when a password
is configured — the bootstrap superuser.

Re-runnable by design. Every migration and upgrade path runs this again, so it
must converge rather than duplicate or overwrite: new permissions are added,
role grants are reconciled to the catalogue, and an existing admin's password is
never reset.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.core.permissions import (
    PERMISSION_CATALOGUE,
    ROLE_DEFINITIONS,
    split_permission,
)
from app.core.security import PasswordHasherService
from app.models.auth import Permission, Role, User
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository

log = get_logger(__name__)


@dataclass(slots=True)
class SeedResult:
    permissions_created: int = 0
    roles_created: int = 0
    role_grants_updated: int = 0
    admin_created: bool = False
    admin_username: str | None = None

    @property
    def changed(self) -> bool:
        return bool(
            self.permissions_created
            or self.roles_created
            or self.role_grants_updated
            or self.admin_created
        )


class SeedService:
    def __init__(
        self,
        users: UserRepository,
        roles: RoleRepository,
        permissions: PermissionRepository,
        hasher: PasswordHasherService,
    ) -> None:
        self._users = users
        self._roles = roles
        self._permissions = permissions
        self._hasher = hasher

    async def seed_permissions(self, result: SeedResult) -> dict[str, Permission]:
        existing = {p.name: p for p in await self._permissions.list_all()}
        for name, description in PERMISSION_CATALOGUE.items():
            if name in existing:
                # Keep descriptions current without touching grants.
                existing[name].description = description
                continue
            resource, action = split_permission(name)
            permission = Permission(
                name=name, resource=resource, action=action, description=description
            )
            self._permissions.add(permission)
            existing[name] = permission
            result.permissions_created += 1
        await self._permissions.flush()
        return existing

    async def seed_roles(
        self, permissions: dict[str, Permission], result: SeedResult
    ) -> dict[str, Role]:
        existing = {r.name: r for r in await self._roles.list_all()}
        for name, (description, permission_names) in ROLE_DEFINITIONS.items():
            granted = [permissions[p] for p in sorted(permission_names) if p in permissions]

            role = existing.get(name)
            if role is None:
                role = Role(name=name, description=description, is_system=True, permissions=granted)
                self._roles.add(role)
                existing[name] = role
                result.roles_created += 1
                continue

            role.description = description
            role.is_system = True
            # Reconcile grants so a permission added to the catalogue in a later
            # release reaches the roles that should hold it. Compared as sets
            # because ordering is meaningless and rewriting an unchanged list
            # would churn the association table on every boot.
            if {p.name for p in role.permissions} != {p.name for p in granted}:
                role.permissions = granted
                result.role_grants_updated += 1

        await self._roles.flush()
        return existing

    async def seed_bootstrap_admin(
        self,
        roles: dict[str, Role],
        *,
        username: str,
        email: str,
        password: str | None,
        result: SeedResult,
    ) -> None:
        """Create the initial superuser if it does not exist.

        An existing account is left completely untouched — re-running the seeder
        must never reset a live administrator's password back to whatever is in
        ``.env``, which would be a silent privilege regression on every upgrade.
        """
        if not password:
            log.info("bootstrap_admin_skipped", reason="no password configured")
            return

        if await self._users.get_by_username(username):
            log.info("bootstrap_admin_exists", username=username)
            return

        super_admin = roles.get("SUPER_ADMIN")
        admin = User(
            username=username,
            email=email,
            full_name="Platform Administrator",
            hashed_password=self._hasher.hash(password),
            is_active=True,
            is_superuser=True,
            roles=[super_admin] if super_admin else [],
        )
        self._users.add(admin)
        await self._users.flush()
        result.admin_created = True
        result.admin_username = username
        log.warning(
            "bootstrap_admin_created",
            username=username,
            note="Change this password immediately after first login.",
        )

    async def run(
        self, *, admin_username: str, admin_email: str, admin_password: str | None
    ) -> SeedResult:
        result = SeedResult()
        permissions = await self.seed_permissions(result)
        roles = await self.seed_roles(permissions, result)
        await self.seed_bootstrap_admin(
            roles,
            username=admin_username,
            email=admin_email,
            password=admin_password,
            result=result,
        )
        log.info(
            "seed_completed",
            permissions_created=result.permissions_created,
            roles_created=result.roles_created,
            role_grants_updated=result.role_grants_updated,
            admin_created=result.admin_created,
        )
        return result
