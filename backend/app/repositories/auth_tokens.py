"""Revoked tokens (E14)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import RevokedToken


class RevokedTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, jti: str) -> RevokedToken | None:
        return await self.session.get(RevokedToken, jti)

    async def is_revoked(self, jti: str) -> bool:
        stmt = select(RevokedToken.jti).where(RevokedToken.jti == jti)
        return (await self.session.execute(stmt)).first() is not None

    async def revoke(
        self,
        *,
        jti: str,
        user_id: uuid.UUID,
        token_type: str,
        reason: str,
        expires_at: dt.datetime,
    ) -> bool:
        """Record ``jti`` as revoked. False if it already was (nothing changes then)."""
        stmt = (
            insert(RevokedToken)
            .values(
                jti=jti,
                user_id=user_id,
                token_type=token_type,
                reason=reason,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=[RevokedToken.jti])
            .returning(RevokedToken.jti)
        )
        return (await self.session.execute(stmt)).first() is not None

    async def prune(self, *, now: dt.datetime) -> int:
        """Forget tokens that have expired anyway."""
        result = await self.session.execute(
            delete(RevokedToken).where(RevokedToken.expires_at < now)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
