"""Voice sessions and configuration (M29, §30).

REST for everything that is not the conversation itself: opening a session, reading what
happened in it, deleting it, and the site-wide settings an administrator controls. The
conversation runs over the WebSocket in `app.api.voice_ws`.

The split in permissions is deliberate. Holding a voice conversation is `agent.execute` —
the same permission as typing to an agent, because it is the same act. Changing which
models the assistant uses is `settings.manage`, because it decides what every session on
the platform sends audio to.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import VoiceConfigStoreDep, VoiceServiceDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.common import MessageResponse
from app.schemas.voice import (
    VoiceConfigResponse,
    VoiceConfigUpdateRequest,
    VoiceSessionCreatedResponse,
    VoiceSessionCreateRequest,
    VoiceSessionDetail,
    VoiceSessionEventRead,
    VoiceSessionMessageRead,
)

router = APIRouter(tags=["voice"])


@router.post(
    "/voice/sessions",
    response_model=VoiceSessionCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open a voice session",
)
async def create_session(
    payload: VoiceSessionCreateRequest,
    service: VoiceServiceDep,
    actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
) -> VoiceSessionCreatedResponse:
    """Open a session and hand back the socket to talk on (§31).

    `agent.execute`, because speaking to an agent is executing it. Voice is a different
    interface to the same act, and giving it a permission of its own would let somebody
    reach by microphone what they may not reach by keyboard.
    """
    session = await service.create(
        actor=actor,
        agent_slug=payload.agent_slug,
        language=payload.language,
        voice=payload.voice,
        conversation_id=payload.conversation_id,
    )
    return VoiceSessionCreatedResponse(
        session_id=session.id,
        agent_id=session.agent_id,
        conversation_id=session.conversation_id,
        language=session.language,
        voice=session.voice,
        state=session.state,
        websocket_url=f"/ws/v1/voice/{session.id}",
    )


@router.get(
    "/voice/sessions/{session_id}",
    response_model=VoiceSessionDetail,
    summary="One voice session, with its turns and timings",
)
async def get_session(
    service: VoiceServiceDep,
    actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
    session_id: uuid.UUID = Path(...),
) -> VoiceSessionDetail:
    session = await service.get(session_id, actor=actor)
    messages = await service.list_messages(session)
    events = await service.list_events(session)
    return VoiceSessionDetail(
        id=session.id,
        agent_id=session.agent_id,
        conversation_id=session.conversation_id,
        language=session.language,
        voice=session.voice,
        state=session.state,
        error=session.error,
        turns=session.turns,
        interruptions=session.interruptions,
        started_at=session.started_at,
        ended_at=session.ended_at,
        messages=[VoiceSessionMessageRead.model_validate(m) for m in messages],
        events=[VoiceSessionEventRead.model_validate(e) for e in events],
    )


@router.delete(
    "/voice/sessions/{session_id}",
    response_model=MessageResponse,
    summary="Delete a voice session and everything said in it",
)
async def delete_session(
    service: VoiceServiceDep,
    actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
    session_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Erase a conversation (§29).

    Available to the person whose voice it was, not only to an administrator: somebody
    who said something they would rather not have recorded should not have to ask.
    """
    session = await service.get(session_id, actor=actor)
    await service.delete(session)
    return MessageResponse(message="Voice session deleted.")


@router.get(
    "/voice/config",
    response_model=VoiceConfigResponse,
    summary="Voice assistant configuration",
)
async def get_config(
    store: VoiceConfigStoreDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
) -> VoiceConfigResponse:
    """Readable by anyone who may hold a conversation.

    The client needs it: which languages are offered, whether interruption is on, and
    whether the module is enabled at all. It carries no secrets — model *aliases*, never
    endpoints or credentials.
    """
    return VoiceConfigResponse(**(await store.get()).model_dump())


@router.put(
    "/voice/config",
    response_model=VoiceConfigResponse,
    summary="Update the voice assistant configuration",
)
async def update_config(
    payload: VoiceConfigUpdateRequest,
    store: VoiceConfigStoreDep,
    _actor: Annotated[User, require_permission(Perm.SETTINGS_MANAGE)],
) -> VoiceConfigResponse:
    """Change it for the whole platform.

    `settings.manage`, not `agent.execute`: this decides what every session on the
    platform sends recorded speech to, and turning retention on is a policy decision
    about other people's voices.
    """
    updated = await store.update(payload.model_dump(exclude_unset=True))
    return VoiceConfigResponse(**updated.model_dump())
