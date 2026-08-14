"""The voice WebSocket (M29, §7).

`/ws/v1/voice/{session_id}` — outside `/api/v1` because it is not a REST resource, and
mounted separately in `app.main` for the same reason `/metrics` is.

**Control is JSON, audio is binary.** A text frame is always a control message and a
binary frame is always PCM. That removes the one ambiguity a mixed protocol has, and it
avoids base64 in both directions — a 33% size penalty on the one payload that is already
the largest thing on the socket.

**Authentication happens before the socket is accepted.** A WebSocket cannot carry an
`Authorization` header from a browser, so the token arrives as a query parameter and is
verified before the upgrade. Rejecting after accepting would mean a socket that looks
open to the client and does nothing, and — worse — a session id that has been confirmed
to exist to somebody who could not open it.

The turn is linear and each stage is timed (§44):

    AUDIO_CHUNK… -> AUDIO_END -> STT -> agent -> TTS -> audio frames out

Streaming TTS as the LLM emits sentences (§21-22) is not implemented here: the platform's
TTS surface synthesises a whole utterance, so a sentence-level pipeline would be latency
theatre against an engine that cannot start early. The seam for it is `_speak`, which is
the only place that would change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.interfaces.agent import RunEventType
from app.core.logging import get_logger
from app.models.voice import VoiceSessionState
from app.services.voice import Stopwatch, TurnTimings

log = get_logger(__name__)

router = APIRouter()

#: Refuse an utterance larger than this rather than buffering it. At 16 kHz 16-bit mono
#: this is about five minutes of speech — far longer than anyone says in one turn, and
#: small enough that a client looping on AUDIO_CHUNK cannot exhaust the process.
MAX_UTTERANCE_BYTES = 16_000 * 2 * 300

#: Outbound audio frame size. Small enough that the browser starts playing quickly,
#: large enough not to drown the socket in frames.
AUDIO_FRAME_BYTES = 32_000


@router.websocket("/ws/v1/voice/{session_id}")
async def voice_socket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    token: str = Query(..., description="Platform access token; a browser cannot set headers"),
) -> None:
    state = websocket.app.state
    sessionmaker: async_sessionmaker = state.database.sessionmaker

    # Verified before the upgrade. See the module docstring.
    try:
        claims = state.token_service.decode(token)
        user_id = uuid.UUID(str(claims.get("sub")))
    except Exception:
        # 1008 = policy violation. Closing before accept means the client never sees an
        # open socket, and never learns whether the session id exists.
        await websocket.close(code=1008)
        return

    handler = _VoiceTurnHandler(websocket, sessionmaker, session_id, user_id)
    await handler.run()


class _VoiceTurnHandler:
    """One socket, one session, many turns.

    Holds no database session between turns: a voice conversation is mostly silence, and
    a transaction held open across it would pin a connection for the length of the call.
    Each turn opens its own.
    """

    def __init__(
        self,
        websocket: WebSocket,
        sessionmaker: async_sessionmaker,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self._ws = websocket
        self._sessionmaker = sessionmaker
        self._session_id = session_id
        self._user_id = user_id
        self._audio = bytearray()
        #: Set when the client interrupts. The speaking loop checks it between frames,
        #: which is what makes barge-in feel immediate (§23).
        self._interrupted = asyncio.Event()
        self._turn: asyncio.Task | None = None

    async def run(self) -> None:
        await self._ws.accept()
        try:
            async with self._sessionmaker() as db:
                service, actor = await _build(db, self._user_id)
                session = await service.get(self._session_id, actor=actor)
                await service.set_state(session, VoiceSessionState.LISTENING)
                await db.commit()

            await self._send(
                "SESSION_STARTED",
                {"session_id": str(self._session_id), "state": "LISTENING"},
            )

            while True:
                message = await self._ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (payload := message.get("bytes")) is not None:
                    await self._on_audio(payload)
                    continue
                text = message.get("text")
                if text is not None and not await self._on_control(text):
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("voice_socket_failed", session=str(self._session_id))
            with contextlib.suppress(Exception):
                await self._send("ERROR", {"message": "The voice session ended unexpectedly."})
        finally:
            await self._close_session()

    # -- inbound -----------------------------------------------------------
    async def _on_audio(self, chunk: bytes) -> None:
        if len(self._audio) + len(chunk) > MAX_UTTERANCE_BYTES:
            # Dropped rather than truncated silently: a caller that keeps sending has a
            # bug, and half an utterance transcribes into something nobody said.
            await self._send("ERROR", {"message": "That utterance was too long."})
            self._audio.clear()
            return
        self._audio.extend(chunk)

    async def _on_control(self, raw: str) -> bool:
        """Handle one control frame. Returns False when the socket should close."""
        try:
            message = json.loads(raw)
        except ValueError:
            await self._send("ERROR", {"message": "Malformed control message."})
            return True

        kind = str(message.get("type") or "").upper()

        if kind == "AUDIO_START":
            self._audio.clear()
            self._interrupted.clear()
            await self._send("LISTENING_STARTED", {})
            return True

        if kind == "AUDIO_END":
            audio = bytes(self._audio)
            self._audio.clear()
            if not audio:
                await self._send("ERROR", {"message": "No audio was received."})
                return True
            # In a task, so INTERRUPT keeps being read while the turn runs. Awaiting the
            # turn here would make barge-in impossible: the socket would not be listening
            # during the only period when somebody wants to interrupt.
            self._turn = asyncio.create_task(self._handle_turn(audio))
            return True

        if kind == "INTERRUPT":
            self._interrupted.set()
            if self._turn and not self._turn.done():
                self._turn.cancel()
            await self._send("ASSISTANT_INTERRUPTED", {})
            async with self._sessionmaker() as db:
                service, actor = await _build(db, self._user_id)
                session = await service.get(self._session_id, actor=actor)
                await service.record_interruption(session)
                await service.set_state(session, VoiceSessionState.LISTENING)
                await db.commit()
            return True

        if kind == "SESSION_END":
            return False

        await self._send("ERROR", {"message": f"Unknown message type {kind!r}."})
        return True

    # -- the turn ----------------------------------------------------------
    async def _handle_turn(self, audio: bytes) -> None:
        timings = TurnTimings()
        try:
            async with self._sessionmaker() as db:
                service, actor = await _build(db, self._user_id)
                session = await service.get(self._session_id, actor=actor)

                transcript = await self._transcribe(db, service, session, audio, timings)
                if transcript is None:
                    await db.commit()
                    return

                answer, run_id = await self._think(db, service, session, actor, transcript, timings)
                await db.commit()

            if answer is None:
                return

            async with self._sessionmaker() as db:
                service, actor = await _build(db, self._user_id)
                session = await service.get(self._session_id, actor=actor)
                await self._speak(db, service, session, answer, timings)
                await service.record_message(session, role="assistant", text=answer, run_id=run_id)
                await service.set_state(session, VoiceSessionState.LISTENING)
                await service.record_event(
                    session,
                    "TURN_COMPLETED",
                    {
                        "stt_ms": timings.stt_ms,
                        "agent_ms": timings.agent_ms,
                        "tts_ms": timings.tts_ms,
                    },
                    duration_ms=timings.total_ms,
                )
                await db.commit()
        except asyncio.CancelledError:
            # Interrupted mid-turn. Expected, not an error.
            raise
        except Exception:
            log.exception("voice_turn_failed", session=str(self._session_id))
            await self._send("ERROR", {"message": "Something went wrong handling that."})

    async def _transcribe(
        self,
        db: AsyncSession,
        service: Any,
        session: Any,
        audio: bytes,
        timings: TurnTimings,
    ) -> str | None:
        from app.services.llm_provider import ProviderError

        await service.set_state(session, VoiceSessionState.TRANSCRIBING)
        watch = Stopwatch()
        gateway = await _gateway(db, self._ws.app.state)
        try:
            result = await gateway.transcribe(
                audio,
                {
                    "model": (await _config(db)).stt_model,
                    # `auto` means omit, so the engine detects. Forcing a language does
                    # not fail on the wrong one, it returns fluent nonsense (§25).
                    "language": None if session.language == "auto" else session.language,
                    "response_format": "verbose_json",
                },
                _context(),
                filename="utterance.wav",
            )
        except (ProviderError, Exception) as exc:
            timings.stt_ms = watch.ms()
            await service.record_event(session, "STT_FAILED", {"error": str(exc)[:200]})
            await self._send(
                "ERROR", {"message": "I couldn't understand the audio. Please try again."}
            )
            await service.set_state(session, VoiceSessionState.LISTENING)
            return None

        timings.stt_ms = watch.ms()
        transcript = str(result.get("text") or "").strip()
        await service.record_event(
            session, "STT_COMPLETED", {"characters": len(transcript)}, duration_ms=timings.stt_ms
        )
        if not transcript:
            await self._send("ERROR", {"message": "I didn't catch that."})
            await service.set_state(session, VoiceSessionState.LISTENING)
            return None

        language = result.get("language")
        await self._send("TRANSCRIPT_FINAL", {"text": transcript, "language": language})
        await service.record_message(
            session,
            role="user",
            text=transcript,
            language=language,
            audio_seconds=result.get("duration"),
        )
        return transcript

    async def _think(
        self,
        db: AsyncSession,
        service: Any,
        session: Any,
        actor: Any,
        transcript: str,
        timings: TurnTimings,
    ) -> tuple[str | None, uuid.UUID]:
        """Run the agent, relaying its §11 events as safe status (§10).

        Chain-of-thought never reaches the client. What is relayed is which tool is
        running and whether it worked — enough for the UI to show progress, and nothing
        the model reasoned on the way there.
        """
        from app.api.deps import build_agent_run_service
        from app.repositories.agents import AgentRepository

        await service.set_state(session, VoiceSessionState.THINKING)
        await self._send("AGENT_STARTED", {})
        watch = Stopwatch()

        agent = await AgentRepository(db).get(session.agent_id)
        if agent is None:  # pragma: no cover — the FK makes this unreachable
            raise RuntimeError(f"Voice session {session.id} references a missing agent.")
        # The same construction a request uses, so voice cannot diverge from text on
        # authorisation, memory or retrieval. See build_agent_run_service.
        runs = build_agent_run_service(db, self._ws.app.state)
        run, stream = await runs.start(
            agent,
            message=transcript,
            actor=actor,
            conversation_id=session.conversation_id,
        )

        answer: str | None = None
        async for event in stream:
            if self._interrupted.is_set():
                break
            if event.type == RunEventType.TOOL_REQUESTED:
                await service.set_state(session, VoiceSessionState.TOOL_EXECUTION)
                await self._send("TOOL_STARTED", {"tool": event.payload.get("tool")})
            elif event.type == RunEventType.TOOL_EXECUTED:
                await self._send(
                    "TOOL_COMPLETED",
                    {
                        "tool": event.payload.get("tool"),
                        "success": event.payload.get("success"),
                    },
                )
            elif event.type == RunEventType.TOOL_APPROVAL_REQUIRED:
                # Voice does not get to skip approval (§41, §42). The run suspends and
                # the client is told to confirm — through the UI, which is where a
                # HIGH-risk action should be confirmed.
                await self._send(
                    "APPROVAL_REQUIRED",
                    {"tool": event.payload.get("tool"), "run_id": str(run.id)},
                )
            elif event.type == RunEventType.RUN_COMPLETED:
                answer = str(event.payload.get("output") or "")
            elif event.type == RunEventType.RUN_FAILED:
                await self._send(
                    "ERROR", {"message": "The assistant could not complete that request."}
                )

        timings.agent_ms = watch.ms()
        await service.record_event(
            session, "AGENT_COMPLETED", {"run_id": str(run.id)}, duration_ms=timings.agent_ms
        )
        if answer:
            await self._send("RESPONSE_TEXT_FINAL", {"text": answer})
        return answer, run.id

    async def _speak(
        self,
        db: AsyncSession,
        service: Any,
        session: Any,
        text: str,
        timings: TurnTimings,
    ) -> None:
        """Synthesise and stream the answer.

        A TTS failure degrades to text rather than ending the turn (§39): the client
        already has RESPONSE_TEXT_FINAL, so the answer is on screen even when nothing
        can be played.
        """
        await service.set_state(session, VoiceSessionState.SYNTHESIZING)
        watch = Stopwatch()
        gateway = await _gateway(db, self._ws.app.state)
        try:
            audio, audio_format = await gateway.synthesize(
                {
                    "model": (await _config(db)).tts_model,
                    "input": text,
                    "voice": session.voice or "alloy",
                    "response_format": "wav",
                },
                _context(),
            )
        except Exception as exc:
            timings.tts_ms = watch.ms()
            await service.record_event(session, "TTS_FAILED", {"error": str(exc)[:200]})
            # Not an ERROR event: the turn succeeded, only the voice is missing.
            await self._send("AUDIO_UNAVAILABLE", {"reason": "speech synthesis failed"})
            return

        timings.tts_ms = watch.ms()
        await service.record_event(
            session, "TTS_COMPLETED", {"bytes": len(audio)}, duration_ms=timings.tts_ms
        )
        await service.set_state(session, VoiceSessionState.SPEAKING)
        await self._send("AUDIO_START", {"format": audio_format, "bytes": len(audio)})
        for offset in range(0, len(audio), AUDIO_FRAME_BYTES):
            if self._interrupted.is_set():
                # Stop mid-stream. The frames already sent are playing; the client stops
                # them on ASSISTANT_INTERRUPTED.
                break
            await self._ws.send_bytes(audio[offset : offset + AUDIO_FRAME_BYTES])
        await self._send("AUDIO_END", {})

    # -- plumbing ----------------------------------------------------------
    async def _send(self, event_type: str, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self._ws.send_text(json.dumps({"type": event_type, **payload}))

    async def _close_session(self) -> None:
        with contextlib.suppress(Exception):
            async with self._sessionmaker() as db:
                service, actor = await _build(db, self._user_id)
                session = await service.get(self._session_id, actor=actor)
                await service.end(session, state=VoiceSessionState.COMPLETED)
                await db.commit()


async def _build(db: AsyncSession, user_id: uuid.UUID) -> tuple[Any, Any]:
    """The service and the acting user, for one turn's session."""
    from app.config.settings import get_settings
    from app.repositories.agents import AgentRepository
    from app.repositories.user import UserRepository
    from app.repositories.voice import (
        VoiceEventRepository,
        VoiceMessageRepository,
        VoiceSessionRepository,
    )
    from app.services.voice import VoiceSessionService
    from app.services.voice_config import VoiceConfigStore

    settings = get_settings()
    actor = await UserRepository(db).get_with_roles(user_id)
    # The effective configuration, so a change made in the admin console reaches the next
    # turn rather than the next restart (§49).
    config = await VoiceConfigStore(db, settings).get()
    service = VoiceSessionService(
        config,
        VoiceSessionRepository(db),
        VoiceMessageRepository(db),
        VoiceEventRepository(db),
        AgentRepository(db),
    )
    return service, actor


async def _config(db: AsyncSession) -> Any:
    """The voice configuration in force for this turn."""
    from app.config.settings import get_settings
    from app.services.voice_config import VoiceConfigStore

    return await VoiceConfigStore(db, get_settings()).get()


async def _gateway(db: AsyncSession, app_state: Any) -> Any:
    """A gateway bound to this turn's session.

    Built per turn rather than held: it carries repositories, and one kept across a
    conversation would pin a database connection through every silence.
    """
    from app.config.settings import get_settings
    from app.repositories.models_registry import (
        ApiKeyRepository,
        ModelAliasRepository,
        ModelDeploymentRepository,
        ModelRepository,
        UsageRepository,
    )
    from app.services.gateway import GatewayService

    return GatewayService(
        get_settings(),
        ModelRepository(db),
        ModelAliasRepository(db),
        ModelDeploymentRepository(db),
        ApiKeyRepository(db),
        UsageRepository(db),
        app_state.redis.client,
        app_state.database.sessionmaker,
    )


def _context() -> Any:
    """Gateway context for a voice call.

    No API key: the caller is a signed-in person on a WebSocket, not an application
    holding a credential. Usage is still recorded, attributed to the session's user.
    """
    from app.services.gateway import GatewayContext

    return GatewayContext()
