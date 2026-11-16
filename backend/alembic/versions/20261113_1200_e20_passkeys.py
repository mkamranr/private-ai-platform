"""e20: passkeys and trusted browsers for the two-factor step

``user_passkeys``: WebAuthn credentials (public key, signature counter) that can answer
the two-factor step instead of an authenticator code.
``trusted_devices``: browsers that may skip the step until ``expires_at``. Only a hash of
their token is stored, and a row is good only for the account's current
``session_version``.

Revision ID: a8c4e2f6b9d1
Revises: f7b2d4a6c9e3
Create Date: 2026-11-13 12:00:00.000000

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a8c4e2f6b9d1"
down_revision: str | None = "f7b2d4a6c9e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_passkeys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "transports",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("credential_id", name="uq_user_passkeys_credential_id"),
    )
    op.create_index("ix_user_passkeys_user_id", "user_passkeys", ["user_id"])

    op.create_table(
        "trusted_devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_trusted_devices_token_hash"),
    )
    op.create_index("ix_trusted_devices_user_id", "trusted_devices", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_trusted_devices_user_id", table_name="trusted_devices")
    op.drop_table("trusted_devices")
    op.drop_index("ix_user_passkeys_user_id", table_name="user_passkeys")
    op.drop_table("user_passkeys")
