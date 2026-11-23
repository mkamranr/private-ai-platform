"""E71: sub-workflows and loops.

* ``workflow_runs.parent_run_id`` / ``parent_node_id``: a run started by a ``workflow`` or
  ``foreach`` step of another run, and which step. The parent continues when it finishes.
* ``workflow_runs.depth``: how deeply it is nested (0 for a run started any other way),
  bounded so a workflow that calls itself stops.
* ``trigger`` gains ``workflow``.

Revision ID: e3b5d7f9a1c2
Revises: d2a4c6e8f0b1
Create Date: 2026-11-25 09:00:00

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3b5d7f9a1c2"
down_revision: str | None = "d2a4c6e8f0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "trigger IN ('manual','api','schedule','webhook','test')"
_NEW = "trigger IN ('manual','api','schedule','webhook','test','workflow')"


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("parent_run_id", sa.UUID(), nullable=True))
    op.add_column("workflow_runs", sa.Column("parent_node_id", sa.String(64), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("depth", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_workflow_runs_parent_run_id_workflow_runs"),
        "workflow_runs",
        "workflow_runs",
        ["parent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_workflow_runs_parent_run_id"), "workflow_runs", ["parent_run_id"], unique=False
    )
    op.drop_constraint(op.f("ck_workflow_runs_trigger_valid"), "workflow_runs", type_="check")
    op.create_check_constraint(op.f("ck_workflow_runs_trigger_valid"), "workflow_runs", _NEW)


def downgrade() -> None:
    op.execute("DELETE FROM workflow_runs WHERE trigger = 'workflow'")
    op.drop_constraint(op.f("ck_workflow_runs_trigger_valid"), "workflow_runs", type_="check")
    op.create_check_constraint(op.f("ck_workflow_runs_trigger_valid"), "workflow_runs", _OLD)
    op.drop_index(op.f("ix_workflow_runs_parent_run_id"), table_name="workflow_runs")
    op.drop_constraint(
        op.f("fk_workflow_runs_parent_run_id_workflow_runs"), "workflow_runs", type_="foreignkey"
    )
    op.drop_column("workflow_runs", "depth")
    op.drop_column("workflow_runs", "parent_node_id")
    op.drop_column("workflow_runs", "parent_run_id")
