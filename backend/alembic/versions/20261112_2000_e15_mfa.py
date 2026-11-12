"""e15: two-factor authentication with an authenticator app

``users.mfa_secret_encrypted``: the TOTP secret, Fernet-encrypted.
``users.mfa_enabled_at``: set once the first code was confirmed; sign-in then asks for one.
``users.mfa_last_step``: the last accepted time step, so a code works once.
``users.mfa_recovery_hashes``: SHA-256 hashes of the unused recovery codes.

Revision ID: e6a1c3f5b8d2
Revises: d5f9b2a3e7c1
Create Date: 2026-11-12 20:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e6a1c3f5b8d2"
down_revision: str | None = "d5f9b2a3e7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("mfa_secret_encrypted", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("mfa_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("mfa_last_step", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "mfa_recovery_hashes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "mfa_recovery_hashes")
    op.drop_column("users", "mfa_last_step")
    op.drop_column("users", "mfa_enabled_at")
    op.drop_column("users", "mfa_secret_encrypted")
