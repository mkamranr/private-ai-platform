"""Alembic environment.

Runs migrations over the **async** engine (asyncpg) rather than adding psycopg2.
A second PostgreSQL driver would mean another wheel in the offline bundle, another
component to security-review and another version to keep aligned — for no benefit,
since Alembic's async recipe is straightforward. ``connection.run_sync`` bridges
Alembic's synchronous migration API onto the async connection.

The DSN comes from ``app.config.settings``, so credentials stay out of the
committed ``alembic.ini`` (§25, Rule 5).

``app.models`` is imported for its side effect of populating ``Base.metadata``.
Every model must be re-exported there — a model absent from the metadata looks
"dropped" to autogenerate, which will then cheerfully write a migration deleting
its table.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config.settings import get_settings
from app.db.base import Base

# Populates Base.metadata. Do not remove: F401 is intentional here.
import app.models  # noqa: F401  # isort: skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database.dsn)

target_metadata = Base.metadata


def _include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Filter objects out of autogenerate.

    Phase 4 introduces LangGraph's PostgreSQL checkpointer, which manages its own
    tables in this database. They are not in ``Base.metadata``, so autogenerate
    would emit DROP statements for them — destroying suspended agent runs mid-flight.
    Excluding them by prefix now means that never happens.
    """
    externally_managed = (
        type_ == "table" and bool(name) and name.startswith(("checkpoint", "langgraph_"))
    )
    return not externally_managed


_CONFIGURE_OPTS = {
    "target_metadata": target_metadata,
    # Detects column type and server-default drift, which autogenerate ignores by
    # default — the source of "the migration ran but the column is still the old
    # type" surprises.
    "compare_type": True,
    "compare_server_default": True,
    "include_object": _include_object,
}


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    How an air-gapped site reviews a migration before applying it, and how a
    DBA-gated upgrade produces a script to hand over (M23).
    """
    context.configure(
        url=settings.database.dsn,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_CONFIGURE_OPTS,  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        # Wrap each migration in its own transaction so a failure leaves no partial
        # schema. PostgreSQL has transactional DDL; this makes use of it.
        transaction_per_migration=True,
        **_CONFIGURE_OPTS,  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
