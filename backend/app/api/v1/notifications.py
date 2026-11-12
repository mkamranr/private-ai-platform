"""Alert notification channels (expansion E13). See `app.services.notifications`."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from app.api.deps import NotificationServiceDep, SettingsDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.notifications import NotificationChannel
from app.schemas.common import Page
from app.schemas.notifications import (
    ChannelCreate,
    ChannelRead,
    ChannelsResponse,
    ChannelTestResult,
    ChannelUpdate,
    DeliveryRead,
)
from app.services.notifications import KNOWN_SOURCES

router = APIRouter(prefix="/notifications", tags=["notifications"], route_class=CommittingRoute)


def _read(channel: NotificationChannel, pending: int) -> ChannelRead:
    read = ChannelRead.model_validate(channel)
    read.pending = pending
    return read


@router.get("/channels", response_model=ChannelsResponse, summary="List notification channels")
async def list_channels(
    service: NotificationServiceDep,
    settings: SettingsDep,
    _actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
) -> ChannelsResponse:
    """Webhook URLs and secrets are never returned: only the host, and whether a secret
    is set."""
    return ChannelsResponse(
        smtp_configured=service.smtp_configured,
        console_url_configured=bool(settings.notifications.console_url),
        sources=list(KNOWN_SOURCES),
        items=[_read(c, n) for c, n in await service.list_channels()],
    )


@router.post(
    "/channels",
    response_model=ChannelRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a notification channel",
)
async def create_channel(
    payload: ChannelCreate,
    service: NotificationServiceDep,
    actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
) -> ChannelRead:
    """A webhook needs `url` (checked against the outbound URL policy); an email channel
    needs `recipients`. Alerts raised before the channel exists are not sent. Audited."""
    channel = await service.create(payload.model_dump(), actor=actor)
    return _read(channel, 0)


@router.patch(
    "/channels/{channel_id}", response_model=ChannelRead, summary="Change a notification channel"
)
async def update_channel(
    payload: ChannelUpdate,
    service: NotificationServiceDep,
    actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
    channel_id: uuid.UUID = Path(...),
) -> ChannelRead:
    """Turning a channel off cancels its queued messages; turning it on starts from now.
    Audited, without secrets."""
    channel = await service.update(channel_id, payload.model_dump(exclude_unset=True), actor=actor)
    return _read(channel, await service.pending_for(channel.id))


@router.delete(
    "/channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove a notification channel",
)
async def delete_channel(
    service: NotificationServiceDep,
    actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
    channel_id: uuid.UUID = Path(...),
) -> Response:
    """Its delivery history goes with it. Audited."""
    await service.delete(channel_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/channels/{channel_id}/test",
    response_model=ChannelTestResult,
    summary="Send a test notification",
)
async def test_channel(
    service: NotificationServiceDep,
    actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
    channel_id: uuid.UUID = Path(...),
) -> ChannelTestResult:
    """Sends now and waits for the answer. A failure is a 200 with `ok: false` and the
    reason; it is recorded as a delivery and in the audit log."""
    result = await service.test(channel_id, actor=actor)
    return ChannelTestResult(ok=result.ok, detail=result.detail, code=result.code)


@router.get("/deliveries", response_model=Page[DeliveryRead], summary="Recent deliveries")
async def list_deliveries(
    service: NotificationServiceDep,
    _actor: Annotated[User, require_permission(Perm.NOTIFICATION_MANAGE)],
    channel_id: Annotated[uuid.UUID | None, Query()] = None,
    delivery_status: Annotated[
        str | None, Query(alias="status", description="pending, sent, failed or cancelled")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DeliveryRead]:
    items, total = await service.list_deliveries(
        channel_id=channel_id, status=delivery_status, limit=limit, offset=offset
    )
    reads = []
    for delivery, name in items:
        read = DeliveryRead.model_validate(delivery)
        read.channel_name = name
        reads.append(read)
    return Page[DeliveryRead](items=reads, total=total, limit=limit, offset=offset)
