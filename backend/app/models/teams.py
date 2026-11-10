"""Teams and team membership (expansion E3, plan §5.12).

A team is the unit of isolation for shared resources: models, aliases, API clients (and
so their keys), agents and knowledge bases carry an optional ``team_id``.

**NULL means shared**, and that is what every row created before E3 is. Making the
column nullable rather than backfilling a "default team" is what keeps the migration
from changing anyone's access: until an operator assigns a resource to a team, the
platform behaves exactly as it did.

A team belongs to a tenant — the same free-form ``tenant_id`` the knowledge tables
already carry — so a later multi-tenant install can group teams without another
migration on every scoped table.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TeamRole(enum.StrEnum):
    MEMBER = "member"
    #: May manage the team's membership without the platform-wide `team.manage`.
    OWNER = "owner"


class Team(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]*$'", name="slug_format"),)

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    #: Stable, URL-safe identifier — what quotas and the UI refer to.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    memberships: Mapped[list[TeamMembership]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Team {self.slug}>"


class TeamMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "team_memberships"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id"),
        CheckConstraint("role IN ('member','owner')", name="role_valid"),
        # "Which teams is this user in?" runs on every authenticated request.
        Index("ix_team_memberships_user", "user_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), default=TeamRole.MEMBER, nullable=False)

    team: Mapped[Team] = relationship(back_populates="memberships")

    def __repr__(self) -> str:
        return f"<TeamMembership {self.team_id} {self.user_id} {self.role}>"
