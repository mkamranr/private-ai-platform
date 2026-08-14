"""Voice sessions (M29).

**This module is a client of the agent engine, not a second one** (§3, §53). It owns the
session, the audio and the socket; everything from the transcript onwards is the same
`AgentRunService` a typed chat uses, which is what makes every existing agent voice-capable
without being rebuilt for voice. A parallel voice-agent would drift from the real one, and
the first thing to drift would be the §10 authorisation pipeline.

The turn is deliberately linear:

    audio in -> STT -> agent run -> text -> TTS -> audio out

Each stage is timed and recorded as a `voice_event`, because "the assistant felt slow" is
not actionable and "TTS took 3.1 s of a 4.2 s turn" is (§44, §45).

**Nothing here bypasses a permission.** The run is started as the signed-in user, so tool
authorisation is the intersection of the agent's grants and theirs, and a HIGH-risk call
suspends for approval exactly as it would in text (§41). Voice changes the interface, not
the rules.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.config.settings import VoiceSettings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.auth import User
from app.models.voice import (
    TERMINAL_VOICE_STATES,
    VoiceEvent,
    VoiceMessage,
    VoiceSession,
    VoiceSessionState,
)
from app.repositories.agents import AgentRepository
from app.repositories.voice import (
    VoiceEventRepository,
    VoiceMessageRepository,
    VoiceSessionRepository,
)

log = get_logger(__name__)


@dataclass(slots=True)
class TurnTimings:
    """Per-stage latency for one turn (§45).

    Separate fields rather than a total: the components have different causes and
    different fixes, and a single number hides which one to look at.
    """

    stt_ms: float = 0.0
    agent_ms: float = 0.0
    tts_ms: float = 0.0
    tool_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return round(self.stt_ms + self.agent_ms + self.tts_ms, 2)


class VoiceSessionService:
    def __init__(
        self,
        config: VoiceSettings,
        sessions: VoiceSessionRepository,
        messages: VoiceMessageRepository,
        events: VoiceEventRepository,
        agents: AgentRepository,
    ) -> None:
        self._config = config
        self._sessions = sessions
        self._messages = messages
        self._events = events
        self._agents = agents

    # -- lifecycle ---------------------------------------------------------
    async def create(
        self,
        *,
        actor: User,
        agent_slug: str | None,
        language: str | None,
        voice: str | None,
        conversation_id: str | None = None,
    ) -> VoiceSession:
        """Open a session against an agent (§31).

        Refused when the module is disabled rather than quietly returning a session that
        cannot do anything: a site that has not deployed speech models should get a clear
        answer, not a socket that fails at the first utterance.
        """
        if not self._config.enabled:
            raise ConflictError(
                "The voice assistant is disabled. Enable it under Settings -> Voice "
                "Assistant once a speech-to-text and a text-to-speech model are deployed."
            )

        slug = agent_slug or self._config.default_agent_slug
        if not slug:
            raise ValidationError(
                "No agent was given and no default is configured. Choose an agent, or set "
                "one under Settings -> Voice Assistant.",
                details={"field": "agent_slug"},
            )
        agent = await self._agents.get_by_slug(slug)
        if agent is None:
            raise NotFoundError(f"No agent with slug {slug!r}.")
        if not agent.enabled:
            raise ConflictError(f"The agent '{agent.slug}' is disabled.")

        session = VoiceSession(
            user_id=actor.id,
            agent_id=agent.id,
            # One conversation per session by default, so the second question in a session
            # resolves against the first (§24). A caller may pass an existing conversation
            # to continue one started in the chat UI.
            conversation_id=conversation_id or f"voice-{uuid.uuid4().hex[:12]}",
            language=language or self._config.default_language,
            voice=voice or self._config.default_voice or None,
            state=VoiceSessionState.CREATED,
        )
        self._sessions.add(session)
        await self._sessions.flush()
        await self.record_event(session, "SESSION_STARTED", {"agent": agent.slug})
        return session

    async def get(self, session_id: uuid.UUID, *, actor: User) -> VoiceSession:
        """One session, if it belongs to the caller.

        A voice session carries what somebody said out loud. Reading another person's is
        not a lesser version of reading their chat history — it is the same thing, so the
        owner check is here rather than left to a permission that merely says "voice".
        """
        session = await self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"No voice session with id {session_id}.")
        if session.user_id != actor.id and not actor.is_superuser:
            raise NotFoundError(f"No voice session with id {session_id}.")
        return session

    async def set_state(self, session: VoiceSession, state: VoiceSessionState) -> None:
        session.state = state
        session.last_activity_at = dt.datetime.now(dt.UTC)
        await self._sessions.flush()

    async def end(
        self, session: VoiceSession, *, state: VoiceSessionState, error: str | None = None
    ) -> VoiceSession:
        if session.state in TERMINAL_VOICE_STATES:
            return session
        session.state = state
        session.error = error
        session.ended_at = dt.datetime.now(dt.UTC)
        await self._sessions.flush()
        await self.record_event(session, "SESSION_ENDED", {"state": str(state)})
        return session

    # -- recording ---------------------------------------------------------
    async def record_event(
        self,
        session: VoiceSession,
        event_type: str,
        payload: dict | None = None,
        *,
        duration_ms: float | None = None,
    ) -> None:
        """Append one event. Never raises into the turn.

        A failure to record telemetry must not end a conversation — the person is mid
        sentence, and the recovery for a missing row is to look at the next one.
        """
        try:
            self._events.add(
                VoiceEvent(
                    session_id=session.id,
                    event_type=event_type,
                    duration_ms=duration_ms,
                    payload=payload or {},
                )
            )
            await self._events.flush()
        except Exception:  # pragma: no cover - defensive
            log.warning("voice_event_not_recorded", session=str(session.id), type=event_type)

    async def record_message(
        self,
        session: VoiceSession,
        *,
        role: str,
        text: str | None,
        language: str | None = None,
        run_id: uuid.UUID | None = None,
        audio_seconds: float | None = None,
    ) -> VoiceMessage:
        """Append a turn.

        The text is dropped when transcript retention is off (§29): the row still records
        that a turn happened and how long it took, which is what the operator needs, while
        what was said is not kept.
        """
        session.turns += 1
        message = VoiceMessage(
            session_id=session.id,
            sequence=session.turns,
            role=role,
            text=text if self._config.store_transcripts else None,
            language=language,
            run_id=run_id,
            audio_seconds=audio_seconds,
        )
        self._messages.add(message)
        await self._messages.flush()
        return message

    async def record_interruption(self, session: VoiceSession) -> None:
        session.interruptions += 1
        await self.set_state(session, VoiceSessionState.INTERRUPTED)
        await self.record_event(session, "INTERRUPTED")

    # -- queries -----------------------------------------------------------
    async def list_messages(self, session: VoiceSession) -> list[VoiceMessage]:
        return list(await self._messages.list_for_session(session.id))

    async def list_events(self, session: VoiceSession) -> list[VoiceEvent]:
        return list(await self._events.list_for_session(session.id))

    async def delete(self, session: VoiceSession) -> None:
        """Remove a session and everything said in it (§29).

        Messages and events cascade. Stored audio, if a site enabled it, lives in MinIO
        under the session prefix and is removed by the retention job — deleting the row
        does not reach into object storage from a request.
        """
        await self._sessions.delete(session)


def timed() -> Stopwatch:
    return Stopwatch()


class Stopwatch:
    """Milliseconds since it was started. Used per stage rather than per turn."""

    def __init__(self) -> None:
        self._started = time.perf_counter()

    def ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 2)


async def collect_text(stream: AsyncIterator[str]) -> str:
    """Join a stream of deltas into one string."""
    return "".join([chunk async for chunk in stream])
