"""Repository base (Rule 6).

Repositories own SQL. Services own behaviour. Routers own HTTP. That split is what
makes a service callable from a worker or the CLI with no request in sight, and it
is machine-enforced by the import-linter contracts in ``pyproject.toml``.

Repositories deliberately do **not** commit. The unit of work is the request (or
the worker task), managed by :func:`app.db.session.session_scope`. A repository
that commits on its own would break atomicity across a multi-step service
operation — the case that matters most being "reserve the GPUs and record the
deployment", which must succeed or fail as one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base


# PEP 695 type parameter syntax (Python 3.12+), which the platform targets.
class BaseRepository[ModelT: Base]:
    """Common CRUD for a single model."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        order_by: Any | None = None,
    ) -> Sequence[ModelT]:
        stmt = select(self.model).limit(limit).offset(offset)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(self.model))
        return int(result.scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        """Stage an insert. Not async — no I/O happens until flush."""
        self.session.add(entity)
        return entity

    async def flush(self) -> None:
        """Send pending changes so server-side defaults and ids become readable."""
        await self.session.flush()

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
