"""phase6: federated identity columns on users

Revision ID: 49981d3780de
Revises: a0633acb6664
Create Date: 2026-08-08 19:43:35.908631

Hand-adjusted from autogenerate in two places, both of which would have bitten later:

1. ``auth_provider`` is NOT NULL, and autogenerate emitted it with no default — which
   fails on any database that already has users, i.e. every real one. Added with a
   server default so existing rows become ``local``, which is what they are.
2. A **partial unique index** on ``(auth_provider, external_subject)``. The federation
   lookup matches on that pair, and without the constraint two accounts could claim the
   same directory subject; the lookup would then raise MultipleResultsFound at sign-in
   rather than at the moment the duplicate was created. Partial, because ``NULL`` subject
   is the normal state for every local account.

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "49981d3780de"
down_revision: str | None = "a0633acb6664"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=32),
            nullable=False,
            server_default="local",
        ),
    )
    op.add_column("users", sa.Column("external_subject", sa.String(length=255), nullable=True))
    op.create_index(op.f("ix_users_external_subject"), "users", ["external_subject"], unique=False)
    op.create_index(
        "uq_users_provider_subject",
        "users",
        ["auth_provider", "external_subject"],
        unique=True,
        postgresql_where=sa.text("external_subject IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_provider_subject", table_name="users")
    op.drop_index(op.f("ix_users_external_subject"), table_name="users")
    op.drop_column("users", "external_subject")
    op.drop_column("users", "auth_provider")
