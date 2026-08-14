"""Voice sessions and their event trail (M29).

Two tables, and the split matters. A **session** is the conversation: who, which agent,
which language, what state it is in now. An **event** is one thing that happened during
it, with a timestamp — and that stream is what makes a voice interaction explainable
afterwards, because nobody can replay the audio to find out what went wrong.

**No audio column anywhere.** Raw speech is biometric data and, at 16 kHz PCM, is far too
large for a row. When a site turns retention on it goes to MinIO under the session's own
prefix (§28); the database holds a pointer at most. A blob column here would be a
privacy problem and an operational one in the same field.

The transcript lives on the *message*, not the event, because it is content rather than
telemetry: it is what the person said, it belongs to the conversation, and it is subject
to a retention policy of its own (§29).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class VoiceSessionState(enum.StrEnum):
    """The §6 lifecycle.

    Recorded rather than inferred: a session that ends in TOOL_EXECUTION tells an
    operator the tool never came back, which is invisible if the only states are open
    and closed.
    """

    CREATED = "CREATED"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    GENERATING = "GENERATING"
    SYNTHESIZING = "SYNTHESIZING"
    SPEAKING = "SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"


#: Terminal states. A session in one of these will not change again, which is what lets
#: the reaper close abandoned sessions without racing a live one.
TERMINAL_VOICE_STATES = frozenset({VoiceSessionState.COMPLETED, VoiceSessionState.ERROR})


class VoiceSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One voice conversation (§6)."""

    __tablename__ = "voice_sessions"
    __table_args__ = (
        Index("ix_voice_sessions_user_created", "user_id", "created_at"),
        Index("ix_voice_sessions_state", "state"),
        CheckConstraint(
            "state IN ('CREATED','LISTENING','TRANSCRIBING','THINKING','TOOL_EXECUTION',"
            "'GENERATING','SYNTHESIZING','SPEAKING','INTERRUPTED','COMPLETED','ERROR')",
            name="voice_state_valid",
        ),
    )

    # SET NULL, not CASCADE: deleting a person must not erase the record that a
    # privileged action was taken by voice (§M24). The audit trail outlives the account.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # RESTRICT: the agent that answered is part of what happened here.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False
    )
    #: Threads voice turns into the same conversation memory a typed chat uses (§24), so
    #: "which one is using the most memory?" resolves against what was just said.
    conversation_id: Mapped[str | None] = mapped_column(String(128), index=True)

    language: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    voice: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        String(24), default=VoiceSessionState.CREATED, nullable=False
    )

    #: Why a session ended, when it ended badly. Free text for a human, never shown to
    #: the model.
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    turns: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Counted because it is the honest measure of whether the assistant is any good:
    #: people interrupt what is answering the wrong question (§45).
    interruptions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class VoiceMessage(Base):
    """One turn of the conversation, as text (§27).

    High volume relative to sessions, so a bigserial key and no `updated_at` — the same
    treatment `agent_run_events` gets. A turn is written once and never edited.
    """

    __tablename__ = "voice_messages"
    __table_args__ = (
        Index("ix_voice_messages_session", "session_id", "sequence"),
        CheckConstraint("role IN ('user','assistant')", name="voice_role_valid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("voice_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Nullable because a site may keep the session and discard what was said (§29).
    #: A row with no text still records that a turn happened, and how long it took.
    text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16))
    #: Links a spoken turn to the agent run that answered it, so the §11 trace and the
    #: Tempo spans are reachable from the conversation.
    run_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    audio_seconds: Mapped[float | None] = mapped_column(Float)
    #: Object key in MinIO when audio retention is on; None otherwise. A key, never the
    #: bytes — see the module docstring.
    audio_object: Mapped[str | None] = mapped_column(String(512))
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VoiceEvent(Base):
    """One thing that happened in a session, with its timing (§27, §45).

    This is the module's observability surface, and it is per-stage on purpose: a voice
    interaction that felt slow is useless to debug as a single total. STT, the agent, the
    tools and TTS each fail and each drag, and only a per-stage record says which.
    """

    __tablename__ = "voice_events"
    __table_args__ = (Index("ix_voice_events_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("voice_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: How long the stage took, where a stage has a duration. The latency budget in §44
    #: is checked against these.
    duration_ms: Mapped[float | None] = mapped_column(Float)
    #: Small, structured, and never sensitive: a tool *name* and an outcome, never its
    #: arguments or credentials (§11). What the model was told is in the agent trace,
    #: under the tighter agent permissions.
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
