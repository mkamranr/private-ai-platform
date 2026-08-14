"""phase9: documents can be in OCR

Revision ID: 4668694c1fd1
Revises: 235d5405d188
Create Date: 2026-08-10 17:50:22.918204

Every migration must implement a working ``downgrade``. ``make migrate-roundtrip``
runs upgrade -> downgrade -> upgrade in CI, so an unimplemented downgrade fails the
build rather than being discovered during a rollback at 3am.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "4668694c1fd1"
down_revision: str | None = "235d5405d188"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('UPLOADED','PARSING','CHUNKING','EMBEDDING','INDEXED','FAILED','NO_TEXT')"
_NEW = (
    "status IN ('UPLOADED','PARSING','OCR','CHUNKING','EMBEDDING','INDEXED',"
    "'FAILED','NO_TEXT')"
)


def upgrade() -> None:
    # Recognising text in a scan is its own state, not part of PARSING: it is the slow
    # step, and a document sitting in PARSING for four minutes is indistinguishable from
    # one that is stuck.
    op.drop_constraint("status_valid", "documents", type_="check")
    op.create_check_constraint("status_valid", "documents", _NEW)


def downgrade() -> None:
    # Any document caught mid-OCR would violate the old constraint, so it is moved to
    # NO_TEXT first. That is the honest resting place for it: the OCR did not finish, so
    # there is no text — and re-running ingestion picks it up again.
    op.execute("UPDATE documents SET status = 'NO_TEXT' WHERE status = 'OCR'")
    op.drop_constraint("status_valid", "documents", type_="check")
    op.create_check_constraint("status_valid", "documents", _OLD)
