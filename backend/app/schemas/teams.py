"""Teams and resource scope schemas (E3)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

TeamRoleLiteral = Literal["member", "owner"]
ResourceTypeLiteral = Literal["model", "alias", "api_client", "agent", "knowledge_base"]


class TeamCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Derived from the name when omitted.",
    )
    description: str | None = None


class TeamUpdateRequest(BaseModel):
    """The slug is not editable: quotas and scripts refer to it."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class TeamMemberRead(BaseModel):
    user_id: uuid.UUID
    username: str
    full_name: str | None = None
    role: str
    added_at: dt.datetime | None = None


class TeamRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    tenant_id: str
    member_count: int = 0
    created_at: dt.datetime


class TeamDetail(TeamRead):
    members: list[TeamMemberRead] = Field(default_factory=list)
    #: How many models, aliases, API clients, agents and knowledge bases it owns.
    resources: dict[str, int] = Field(default_factory=dict)


class TeamMemberAddRequest(BaseModel):
    user_id: uuid.UUID
    role: TeamRoleLiteral = "member"


class TeamMemberUpdateRequest(BaseModel):
    role: TeamRoleLiteral


class TeamMemberChange(BaseModel):
    user_id: uuid.UUID
    username: str | None = None
    role: str


class ScopeAssignRequest(BaseModel):
    resource_type: ResourceTypeLiteral
    resource_id: uuid.UUID
    team_id: uuid.UUID | None = Field(
        description="The team to move it to, or null to make it shared."
    )


class ScopeAssignResponse(BaseModel):
    resource_type: str
    resource_id: uuid.UUID
    team_id: uuid.UUID | None = None
    previous_team_id: uuid.UUID | None = None


class MyScopeRead(BaseModel):
    """What the caller can see: every team, or these."""

    all_teams: bool
    teams: list[TeamRead] = Field(default_factory=list)
