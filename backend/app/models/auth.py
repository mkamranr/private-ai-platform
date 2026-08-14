"""Identity and RBAC tables (M03).

RBAC is a three-level model: a user holds roles, a role grants permissions, and
authorisation is always evaluated against the *effective permission set*. Code
never branches on a role name — roles are configuration, permissions are the
contract. That is what lets an operator define a new role in Phase 6 without a
code change, and it is why ``require_permission("model.deploy")`` is the only
authorisation primitive in the codebase.

Enforcement is server-side, always (§M03).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# --- association tables ----------------------------------------------------
# Core Table/Column, not mapped_column: these are pure join tables with no ORM
# entity of their own, and Table() only accepts SchemaItem objects.
#
# ondelete="CASCADE" on both sides so deleting a user or role tidies up its
# memberships. Note the deliberate contrast with audit_logs, which uses SET NULL —
# a membership is disposable, a record of what someone did is not.
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column(
        "user_id",
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        PgUUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column(
        "role_id",
        PgUUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        PgUUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # One directory subject, one account. Without this the federation lookup could
        # find two rows and raise at sign-in, long after the duplicate was created.
        # Partial: a NULL subject is the normal state of every local account.
        Index(
            "uq_users_provider_subject",
            "auth_provider",
            "external_subject",
            unique=True,
            postgresql_where=text("external_subject IS NOT NULL"),
        ),
    )

    username: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))

    # Nullable: an OIDC- or LDAP-provisioned user has no local password. The login path
    # already refuses a null hash, so a federated account cannot be signed into with a
    # password even if one is later guessed at.
    hashed_password: Mapped[str | None] = mapped_column(String(255))

    #: Which provider vouches for this account — "local", or a configured provider name.
    #: Stored rather than inferred from `hashed_password is None`, because "federated" and
    #: "local account whose password was cleared" need different handling and inference
    #: would conflate them.
    auth_provider: Mapped[str] = mapped_column(
        String(32), default="local", server_default="local", nullable=False
    )
    #: The provider's stable subject. Matched on before username, because usernames are
    #: reassigned when people leave and matching on one would hand a new joiner the
    #: previous holder's roles and audit history.
    external_subject: Mapped[str | None] = mapped_column(String(255), index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Bypasses permission checks entirely. Exactly one bootstrap account should
    # have this; everything else goes through roles.
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Supports lockout policy in Phase 6; counted from Phase 0 so the data exists.
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)

    roles: Mapped[list[Role]] = relationship(
        secondary=user_roles,
        back_populates="users",
        lazy="selectin",  # authorisation needs these on essentially every request
    )

    @property
    def effective_permissions(self) -> frozenset[str]:
        """Union of permissions across all roles.

        A superuser implicitly holds everything; callers must check
        :attr:`is_superuser` rather than expecting it enumerated here, because the
        full permission set is not knowable from the user row alone.
        """
        return frozenset(permission.name for role in self.roles for permission in role.permissions)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class Role(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # System roles are seeded and must not be deleted through the API; operators
    # may still create their own.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    users: Mapped[list[User]] = relationship(secondary=user_roles, back_populates="roles")
    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Role {self.name}>"


class Permission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single grant, named ``resource.action`` (§M03)."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("resource", "action", name="resource_action"),)

    # Denormalised full name ("model.deploy") for fast lookup; resource/action are
    # kept split so the admin UI can group permissions without parsing strings.
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    roles: Mapped[list[Role]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )

    def __repr__(self) -> str:
        return f"<Permission {self.name}>"


def permission_uuid() -> uuid.UUID:
    """Explicit id generator, used by the seeder for deterministic reruns."""
    return uuid.uuid4()
