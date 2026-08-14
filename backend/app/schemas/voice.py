"""Voice session and configuration schemas (M29)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class VoiceSessionCreateRequest(BaseModel):
    #: Optional: falls back to the configured default agent, so a client can open a
    #: session without knowing what the site has deployed.
    agent_slug: str | None = Field(default=None, max_length=64)
    language: Literal["auto", "en", "ar"] | None = None
    voice: str | None = Field(default=None, max_length=64)
    #: Continue an existing conversation — the same thread a typed chat would use, so a
    #: person can start in text and finish by voice (§24).
    conversation_id: str | None = Field(default=None, max_length=128)


class VoiceSessionCreatedResponse(BaseModel):
    session_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: str | None
    language: str
    voice: str | None
    state: str
    #: Relative, not absolute: the browser knows its own origin, and a URL built
    #: server-side would have to guess the scheme behind the proxy.
    websocket_url: str


class VoiceSessionMessageRead(ORMModel):
    sequence: int
    role: str
    #: Absent when transcript retention is off (§29). The turn still shows.
    text: str | None = None
    language: str | None = None
    run_id: uuid.UUID | None = None
    audio_seconds: float | None = None
    recorded_at: dt.datetime


class VoiceSessionEventRead(ORMModel):
    event_type: str
    duration_ms: float | None = None
    payload: dict = Field(default_factory=dict)
    recorded_at: dt.datetime


class VoiceSessionDetail(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: str | None
    language: str
    voice: str | None
    state: str
    error: str | None
    turns: int
    interruptions: int
    started_at: dt.datetime
    ended_at: dt.datetime | None
    messages: list[VoiceSessionMessageRead] = Field(default_factory=list)
    events: list[VoiceSessionEventRead] = Field(default_factory=list)


class VoiceConfigResponse(BaseModel):
    """What the assistant is configured to do (§49).

    Model *aliases*, never endpoints or credentials: this is readable by anyone who may
    hold a conversation, and where a model physically runs is not their business (§12).
    """

    enabled: bool
    default_language: str
    stt_model: str
    tts_model: str
    default_voice: str
    default_agent_slug: str
    sample_rate_hz: int
    max_session_seconds: int
    idle_timeout_seconds: int
    interrupt_enabled: bool
    vad_enabled: bool
    store_audio: bool
    store_transcripts: bool
    retention_days: int


class VoiceConfigUpdateRequest(BaseModel):
    """A partial update. Only the fields sent are changed (§49).

    Every field optional, because the admin form saves one section at a time and a full
    replacement would silently reset whatever that section does not show.
    """

    enabled: bool | None = None
    default_language: Literal["auto", "en", "ar"] | None = None
    stt_model: str | None = Field(default=None, max_length=128)
    tts_model: str | None = Field(default=None, max_length=128)
    default_voice: str | None = Field(default=None, max_length=64)
    default_agent_slug: str | None = Field(default=None, max_length=64)
    sample_rate_hz: int | None = Field(default=None, ge=8000, le=48000)
    max_session_seconds: int | None = Field(default=None, ge=30, le=7200)
    idle_timeout_seconds: int | None = Field(default=None, ge=10, le=3600)
    interrupt_enabled: bool | None = None
    vad_enabled: bool | None = None
    store_audio: bool | None = None
    store_transcripts: bool | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
