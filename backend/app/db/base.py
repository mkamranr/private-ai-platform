"""Declarative base, constraint naming and shared column mixins (M01).

The naming convention is not cosmetic. Without it, PostgreSQL invents constraint
names, Alembic autogenerate cannot reliably match an existing constraint to a
model, and every subsequent migration churns — dropping and recreating indexes it
merely failed to recognise. With 28 modules each adding tables, that compounds
fast. It has to be set before the first migration, because changing it later
requires renaming every existing constraint.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """UUID primary keys.

    Chosen over bigserial because ids appear in API paths and audit records, and
    because a multi-node future (V2) makes non-coordinated id generation useful.
    Generated in Python so the object has its id before the flush, which lets a
    service write an audit record referencing a row in the same transaction.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """Server-side created/updated timestamps.

    ``server_default``/``onupdate`` rather than Python defaults so rows written by
    a migration or by psql are stamped too. Always timezone-aware — a platform
    that will run in more than one timezone cannot afford naive datetimes.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
