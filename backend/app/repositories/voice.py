"""Voice session persistence (M29)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.models.voice import (
    TERMINAL_VOICE_STATES,
    VoiceEvent,
    VoiceMessage,
    VoiceSession,
)
from app.repositories.base import BaseRepository


class VoiceSessionRepository(BaseRepository[VoiceSession]):
    model = VoiceSession

    async def list_for_user(self, user_id: uuid.UUID, *, limit: int = 50) -> Sequence[VoiceSession]:
        stmt = (
            select(VoiceSession)
            .where(VoiceSession.user_id == user_id)
            .order_by(VoiceSession.created_at.desc())
            .limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_stale(self, *, older_than: dt.datetime) -> Sequence[VoiceSession]:
        """Sessions still open but silent since ``older_than``.

        A voice session holds a socket and an agent. A browser tab closed without a
        SESSION_END leaves one open for ever, so something has to close them — and it
        must select on *activity*, not on age, or it would kill a long conversation that
        is going perfectly well.
        """
        stmt = select(VoiceSession).where(
            VoiceSession.state.not_in([str(s) for s in TERMINAL_VOICE_STATES]),
            VoiceSession.last_activity_at < older_than,
        )
        return (await self.session.execute(stmt)).scalars().all()


class VoiceMessageRepository(BaseRepository[VoiceMessage]):
    model = VoiceMessage

    async def list_for_session(self, session_id: uuid.UUID) -> Sequence[VoiceMessage]:
        stmt = (
            select(VoiceMessage)
            .where(VoiceMessage.session_id == session_id)
            .order_by(VoiceMessage.sequence)
        )
        return (await self.session.execute(stmt)).scalars().all()


class VoiceEventRepository(BaseRepository[VoiceEvent]):
    model = VoiceEvent

    async def list_for_session(self, session_id: uuid.UUID) -> Sequence[VoiceEvent]:
        stmt = select(VoiceEvent).where(VoiceEvent.session_id == session_id).order_by(VoiceEvent.id)
        return (await self.session.execute(stmt)).scalars().all()
