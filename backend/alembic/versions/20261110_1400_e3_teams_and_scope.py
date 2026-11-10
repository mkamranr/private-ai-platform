"""e3 teams and scope

Additive, and deliberately changes nobody's access. Two new tables (``teams``,
``team_memberships``) and a **nullable** ``team_id`` on models, aliases, API clients,
agents, knowledge bases and usage records. Every existing row keeps ``team_id = NULL``,
which means "shared" — exactly the visibility it had before — so no backfill is needed
and nothing becomes hidden on upgrade. ``gateway_quotas`` gains the ``team`` scope.

Resource foreign keys are RESTRICT: deleting a team that still owns resources would
silently widen them to shared. Usage records use SET NULL, since history outlives a team.

Revision ID: 9d4b2a6c8e51
Revises: 7c2e4f9a1d30
Create Date: 2026-11-10 11:18:05.171884

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d4b2a6c8e51"
down_revision: str | None = "7c2e4f9a1d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]*$'", name=op.f("ck_teams_slug_format")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_teams_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("name", name=op.f("uq_teams_name")),
    )
    op.create_index(op.f("ix_teams_slug"), "teams", ["slug"], unique=True)
    op.create_table(
        "team_memberships",
        sa.Column("team_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('member','owner')", name=op.f("ck_team_memberships_role_valid")
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["teams.id"],
            name=op.f("fk_team_memberships_team_id_teams"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_team_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team_memberships")),
        sa.UniqueConstraint("team_id", "user_id", name=op.f("uq_team_memberships_team_id_user_id")),
    )
    op.create_index("ix_team_memberships_user", "team_memberships", ["user_id"], unique=False)
    op.add_column("agents", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_agents_team_id"), "agents", ["team_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_agents_team_id_teams"), "agents", "teams", ["team_id"], ["id"], ondelete="RESTRICT"
    )
    op.add_column("api_clients", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_api_clients_team_id"), "api_clients", ["team_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_api_clients_team_id_teams"),
        "api_clients",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("knowledge_bases", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_knowledge_bases_team_id"), "knowledge_bases", ["team_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_knowledge_bases_team_id_teams"),
        "knowledge_bases",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("model_aliases", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_model_aliases_team_id"), "model_aliases", ["team_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_model_aliases_team_id_teams"),
        "model_aliases",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column("models", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_models_team_id"), "models", ["team_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_models_team_id_teams"), "models", "teams", ["team_id"], ["id"], ondelete="RESTRICT"
    )
    op.add_column("usage_records", sa.Column("team_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_usage_records_team_recorded", "usage_records", ["team_id", "recorded_at"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_usage_records_team_id_teams"),
        "usage_records",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(op.f("ck_gateway_quotas_scope_valid"), "gateway_quotas", type_="check")
    op.create_check_constraint(
        op.f("ck_gateway_quotas_scope_valid"),
        "gateway_quotas",
        "scope IN ('api_key','end_user','model','team')",
    )


def downgrade() -> None:
    # Team quotas cannot survive without the scope; remove them before narrowing it.
    op.execute("DELETE FROM gateway_quotas WHERE scope = 'team'")
    op.drop_constraint(op.f("ck_gateway_quotas_scope_valid"), "gateway_quotas", type_="check")
    op.create_check_constraint(
        op.f("ck_gateway_quotas_scope_valid"),
        "gateway_quotas",
        "scope IN ('api_key','end_user','model')",
    )
    op.drop_constraint(op.f("fk_usage_records_team_id_teams"), "usage_records", type_="foreignkey")
    op.drop_index("ix_usage_records_team_recorded", table_name="usage_records")
    op.drop_column("usage_records", "team_id")
    op.drop_constraint(op.f("fk_models_team_id_teams"), "models", type_="foreignkey")
    op.drop_index(op.f("ix_models_team_id"), table_name="models")
    op.drop_column("models", "team_id")
    op.drop_constraint(op.f("fk_model_aliases_team_id_teams"), "model_aliases", type_="foreignkey")
    op.drop_index(op.f("ix_model_aliases_team_id"), table_name="model_aliases")
    op.drop_column("model_aliases", "team_id")
    op.drop_constraint(
        op.f("fk_knowledge_bases_team_id_teams"), "knowledge_bases", type_="foreignkey"
    )
    op.drop_index(op.f("ix_knowledge_bases_team_id"), table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "team_id")
    op.drop_constraint(op.f("fk_api_clients_team_id_teams"), "api_clients", type_="foreignkey")
    op.drop_index(op.f("ix_api_clients_team_id"), table_name="api_clients")
    op.drop_column("api_clients", "team_id")
    op.drop_constraint(op.f("fk_agents_team_id_teams"), "agents", type_="foreignkey")
    op.drop_index(op.f("ix_agents_team_id"), table_name="agents")
    op.drop_column("agents", "team_id")
    op.drop_index("ix_team_memberships_user", table_name="team_memberships")
    op.drop_table("team_memberships")
    op.drop_index(op.f("ix_teams_slug"), table_name="teams")
    op.drop_table("teams")
