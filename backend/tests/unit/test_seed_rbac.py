"""`seed --rbac-only` (E11): an upgrade reconciles grants and never creates accounts."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.permissions import ROLE_DEFINITIONS, Permission, Role
from app.core.security import PasswordHasherService
from app.models.auth import User
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository
from app.services.seed import SeedService


def _service(session: AsyncSession, settings: Settings) -> SeedService:
    return SeedService(
        UserRepository(session),
        RoleRepository(session),
        PermissionRepository(session),
        PasswordHasherService(settings.security),
    )


async def test_restores_a_missing_grant_and_creates_no_account(
    session: AsyncSession, settings: Settings
) -> None:
    service = _service(session, settings)
    await service.run_rbac()  # whatever the shared database held, start reconciled

    roles = {r.name: r for r in await RoleRepository(session).list_all()}
    admin = roles[Role.ADMIN]
    assert Permission.COST_VIEW in ROLE_DEFINITIONS[Role.ADMIN][1]
    # What an install from before the permission existed looks like.
    admin.permissions = [p for p in admin.permissions if p.name != Permission.COST_VIEW]
    await session.flush()
    users_before = await session.scalar(select(func.count()).select_from(User))

    result = await service.run_rbac()

    assert result.role_grants_updated >= 1
    assert Permission.COST_VIEW in {p.name for p in admin.permissions}
    assert result.admin_created is False
    assert await session.scalar(select(func.count()).select_from(User)) == users_before


async def test_is_a_no_op_when_already_reconciled(
    session: AsyncSession, settings: Settings
) -> None:
    service = _service(session, settings)
    await service.run_rbac()
    result = await service.run_rbac()
    assert (result.permissions_created, result.roles_created, result.role_grants_updated) == (
        0,
        0,
        0,
    )
