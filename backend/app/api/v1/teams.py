"""Teams, membership and resource scope (E3, plan §5.12).

A team owns models, aliases, API clients (and so their keys), agents and knowledge
bases. Resources with no team are shared. See app/core/scope.py for the rule, and
docs/teams.md for the operator's view.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import (
    AuthServiceDep,
    CurrentUserDep,
    ScopeDep,
    ScopeGuardDep,
    TeamServiceDep,
    require_permission,
)
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.models.auth import User
from app.schemas.common import MessageResponse
from app.schemas.teams import (
    MyScopeRead,
    ScopeAssignRequest,
    ScopeAssignResponse,
    TeamCreateRequest,
    TeamDetail,
    TeamMemberAddRequest,
    TeamMemberChange,
    TeamMemberUpdateRequest,
    TeamRead,
    TeamUpdateRequest,
)

router = APIRouter(tags=["teams"], route_class=CommittingRoute)


@router.get("/teams", response_model=list[TeamRead], summary="List teams")
async def list_teams(
    service: TeamServiceDep,
    _actor: Annotated[User, require_permission(Perm.TEAM_VIEW)],
    scope: ScopeDep,
) -> list[TeamRead]:
    """Every team for a holder of `scope.all`; otherwise the caller's own teams."""
    return [TeamRead(**row) for row in await service.list_teams(scope)]


# Declared before /teams/{team_id}, which would otherwise try to parse "mine" as a UUID.
@router.get("/teams/mine", response_model=MyScopeRead, summary="The caller's teams and scope")
async def my_teams(service: TeamServiceDep, _user: CurrentUserDep, scope: ScopeDep) -> MyScopeRead:
    """Any signed-in user may ask which teams they belong to — the console uses it for the
    team picker on create forms. `all_teams` says whether they can see every team's
    resources regardless."""
    rows = await service.list_teams(Scope(team_ids=scope.team_ids))
    return MyScopeRead(all_teams=scope.all_teams, teams=[TeamRead(**row) for row in rows])


@router.post(
    "/teams", response_model=TeamRead, status_code=status.HTTP_201_CREATED, summary="Create a team"
)
async def create_team(
    payload: TeamCreateRequest,
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_MANAGE)],
) -> TeamRead:
    team = await service.create(
        name=payload.name, slug=payload.slug, description=payload.description, actor=actor
    )
    return TeamRead(**service.describe(team, member_count=0))


@router.get("/teams/{team_id}", response_model=TeamDetail, summary="Get a team")
async def get_team(
    service: TeamServiceDep,
    _actor: Annotated[User, require_permission(Perm.TEAM_VIEW)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
) -> TeamDetail:
    return TeamDetail(**await service.detail(team_id, scope))


@router.patch("/teams/{team_id}", response_model=TeamRead, summary="Rename or describe a team")
async def update_team(
    payload: TeamUpdateRequest,
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_MANAGE)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
) -> TeamRead:
    team = await service.update(
        team_id, payload.model_dump(exclude_unset=True), scope=scope, actor=actor
    )
    members = await service.members(team.id)
    return TeamRead(**service.describe(team, member_count=len(members)))


@router.delete("/teams/{team_id}", response_model=MessageResponse, summary="Delete a team")
async def delete_team(
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_MANAGE)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Refused (409) while the team still owns resources; the error lists them."""
    await service.delete(team_id, scope=scope, actor=actor)
    return MessageResponse(message="Team deleted.")


# ---------------------------------------------------------------------------
# Members — team.manage, or an owner of the team
# ---------------------------------------------------------------------------
@router.post(
    "/teams/{team_id}/members",
    response_model=TeamMemberChange,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member",
)
async def add_member(
    payload: TeamMemberAddRequest,
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_VIEW)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
) -> TeamMemberChange:
    return TeamMemberChange(
        **await service.add_member(
            team_id, user_id=payload.user_id, role=payload.role, scope=scope, actor=actor
        )
    )


@router.patch(
    "/teams/{team_id}/members/{user_id}",
    response_model=TeamMemberChange,
    summary="Change a member's team role",
)
async def update_member(
    payload: TeamMemberUpdateRequest,
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_VIEW)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
) -> TeamMemberChange:
    return TeamMemberChange(
        **await service.update_member(team_id, user_id, role=payload.role, scope=scope, actor=actor)
    )


@router.delete(
    "/teams/{team_id}/members/{user_id}", response_model=MessageResponse, summary="Remove a member"
)
async def remove_member(
    service: TeamServiceDep,
    actor: Annotated[User, require_permission(Perm.TEAM_VIEW)],
    scope: ScopeDep,
    team_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Takes effect on the member's next request: scope is read per request."""
    await service.remove_member(team_id, user_id, scope=scope, actor=actor)
    return MessageResponse(message="Member removed from the team.")


# ---------------------------------------------------------------------------
# Moving a resource between teams
# ---------------------------------------------------------------------------
@router.put("/scope", response_model=ScopeAssignResponse, summary="Move a resource to a team")
async def assign_scope(
    payload: ScopeAssignRequest,
    guard: ScopeGuardDep,
    auth: AuthServiceDep,
    actor: CurrentUserDep,
) -> ScopeAssignResponse:
    """Assign a model, alias, API client, agent or knowledge base to a team, or make it
    shared (`team_id: null`).

    Requires the permission that changes that kind of resource (model.register,
    model.deploy, apikey.manage, agent.edit, knowledge.manage), sight of the resource,
    and membership of both the team it is leaving and the team it is joining — unless
    you hold scope.all.
    """
    return ScopeAssignResponse(
        **await guard.reassign(
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            team_id=payload.team_id,
            actor=actor,
            auth=auth,
        )
    )
