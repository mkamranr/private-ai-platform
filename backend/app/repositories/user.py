"""User, role and permission repositories (M03)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.auth import Permission, Role, User
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    # Roles and their permissions are needed on essentially every authenticated
    # request, so they are eager-loaded here rather than lazily — a lazy load
    # inside an async request raises MissingGreenlet, and the "load it later"
    # version of this bug only shows up under concurrency.
    _EAGER = (selectinload(User.roles).selectinload(Role.permissions),)

    async def get_with_roles(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.id == user_id).options(*self._EAGER)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_username(self, username: str) -> User | None:
        """Case-insensitive lookup.

        Usernames are stored as entered but matched case-insensitively, so
        ``Admin`` and ``admin`` cannot become two accounts.
        """
        stmt = (
            select(User).where(func.lower(User.username) == username.lower()).options(*self._EAGER)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_external_subject(self, provider: str, subject: str) -> User | None:
        """Find a federated account by the provider's own identifier.

        Case-sensitive, unlike the username lookup: a subject is an opaque token from the
        provider, and folding its case could collide two distinct people.
        """
        stmt = (
            select(User)
            .where(User.auth_provider == provider, User.external_subject == subject)
            .options(*self._EAGER)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == email.lower()).options(*self._EAGER)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_with_roles(self, *, limit: int = 50, offset: int = 0) -> Sequence[User]:
        stmt = (
            select(User).options(*self._EAGER).order_by(User.username).limit(limit).offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_name(self, name: str) -> Role | None:
        stmt = select(Role).where(Role.name == name).options(selectinload(Role.permissions))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> Sequence[Role]:
        stmt = select(Role).options(selectinload(Role.permissions)).order_by(Role.name)
        return (await self.session.execute(stmt)).scalars().all()


class PermissionRepository(BaseRepository[Permission]):
    model = Permission

    async def get_by_name(self, name: str) -> Permission | None:
        stmt = select(Permission).where(Permission.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> Sequence[Permission]:
        stmt = select(Permission).order_by(Permission.resource, Permission.action)
        return (await self.session.execute(stmt)).scalars().all()

    async def get_many_by_name(self, names: Sequence[str]) -> Sequence[Permission]:
        """Bulk fetch — used by the seeder to wire roles in one round trip."""
        if not names:
            return []
        stmt = select(Permission).where(Permission.name.in_(names))
        return (await self.session.execute(stmt)).scalars().all()
