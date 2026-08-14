"""User, role and permission administration (M03, §8).

Every route here declares an explicit permission. These are the first routes in the
platform, and they set the pattern each later module follows: no mutating endpoint
without a ``require_permission`` dependency.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import (
    CurrentUserDep,
    UserServiceDep,
    require_permission,
)
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.auth import (
    PasswordChangeRequest,
    PasswordSetRequest,
    PermissionRead,
    RoleRead,
    UserActiveUpdateRequest,
    UserCreateRequest,
    UserRead,
    UserRolesUpdateRequest,
)
from app.schemas.common import MessageResponse, Page

router = APIRouter(tags=["administration"])


@router.get("/users", response_model=Page[UserRead], summary="List users")
async def list_users(
    service: UserServiceDep,
    _actor: Annotated[User, require_permission(Perm.USER_VIEW)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserRead]:
    users = await service.list_users(limit=limit, offset=offset)
    total = await service.count_users()
    return Page[UserRead](
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/users",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
)
async def create_user(
    payload: UserCreateRequest,
    service: UserServiceDep,
    actor: Annotated[User, require_permission(Perm.USER_MANAGE)],
) -> UserRead:
    user = await service.create_user(
        username=payload.username,
        email=str(payload.email),
        password=payload.password,
        full_name=payload.full_name,
        role_names=payload.roles,
        actor=actor,
    )
    return UserRead.model_validate(user)


@router.get("/users/{user_id}", response_model=UserRead, summary="Get a user")
async def get_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
    _actor: Annotated[User, require_permission(Perm.USER_VIEW)],
) -> UserRead:
    return UserRead.model_validate(await service.get_user(user_id))


@router.put(
    "/users/{user_id}/active",
    response_model=UserRead,
    summary="Enable or disable a user",
)
async def set_user_active(
    user_id: uuid.UUID,
    payload: UserActiveUpdateRequest,
    service: UserServiceDep,
    actor: Annotated[User, require_permission(Perm.USER_MANAGE)],
) -> UserRead:
    """Disabling is preferred over deletion — it preserves the audit trail."""
    user = await service.set_active(user_id, active=payload.is_active, actor=actor)
    return UserRead.model_validate(user)


@router.put("/users/{user_id}/roles", response_model=UserRead, summary="Replace a user's roles")
async def set_user_roles(
    user_id: uuid.UUID,
    payload: UserRolesUpdateRequest,
    service: UserServiceDep,
    actor: Annotated[User, require_permission(Perm.ROLE_MANAGE)],
) -> UserRead:
    user = await service.assign_roles(user_id, payload.roles, actor=actor)
    return UserRead.model_validate(user)


@router.put(
    "/users/{user_id}/password",
    response_model=MessageResponse,
    summary="Set a user's password",
)
async def set_user_password(
    user_id: uuid.UUID,
    payload: PasswordSetRequest,
    service: UserServiceDep,
    actor: Annotated[User, require_permission(Perm.USER_MANAGE)],
) -> MessageResponse:
    """Refused for federated accounts — see :meth:`UserService.set_password`."""
    user = await service.set_password(user_id, payload.password, actor=actor)
    return MessageResponse(
        message=f"Password set for {user.username}. Their existing tokens stay valid until "
        "they expire — the platform has no revocation store yet."
    )


@router.post(
    "/users/me/password",
    response_model=MessageResponse,
    summary="Change your own password",
)
async def change_own_password(
    payload: PasswordChangeRequest,
    user: CurrentUserDep,
    service: UserServiceDep,
) -> MessageResponse:
    """No permission required — this is the caller's own account.

    Deliberately not gated on ``user.manage``: someone with no administrative rights at
    all must still be able to change their own password.
    """
    await service.change_own_password(
        user, current_password=payload.current_password, new_password=payload.new_password
    )
    return MessageResponse(message="Password changed.")


@router.get("/roles", response_model=list[RoleRead], summary="List roles and their permissions")
async def list_roles(
    service: UserServiceDep,
    _actor: Annotated[User, require_permission(Perm.USER_VIEW)],
) -> list[RoleRead]:
    return [RoleRead.model_validate(r) for r in await service.list_roles()]


@router.get("/permissions", response_model=list[PermissionRead], summary="List permissions")
async def list_permissions(
    service: UserServiceDep,
    _actor: Annotated[User, require_permission(Perm.USER_VIEW)],
) -> list[PermissionRead]:
    """The full catalogue, for building role-editing UI."""
    return [PermissionRead.model_validate(p) for p in await service.list_permissions()]


@router.get(
    "/whoami",
    response_model=UserRead,
    summary="Authenticated identity without a permission requirement",
)
async def whoami(user: CurrentUserDep) -> UserRead:
    """Useful for verifying a token works when the caller holds no permissions."""
    return UserRead.model_validate(user)
