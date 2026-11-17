"""E35: tools can belong to a team — ``tools.team_id``.

Every existing tool stays shared (NULL), so nothing changes until someone moves one.

Revision ID: d5f7b9c1e3a4
Revises: c4e6a8b0d2f3
Create Date: 2026-11-17 10:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d5f7b9c1e3a4"
down_revision: str | None = "c4e6a8b0d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tools", sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_tools_team_id_teams"), "tools", "teams", ["team_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_tools_team_id"), "tools", ["team_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_tools_team_id"), table_name="tools")
    op.drop_constraint(op.f("fk_tools_team_id_teams"), "tools", type_="foreignkey")
    op.drop_column("tools", "team_id")
