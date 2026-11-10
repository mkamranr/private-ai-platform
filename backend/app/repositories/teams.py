"""Team and membership repositories (E3)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agents import Agent, AgentRun
from app.models.auth import User
from app.models.knowledge import Document, KnowledgeBase
from app.models.models_registry import ApiClient, ApiKey, Model, ModelAlias, ModelDeployment
from app.models.teams import Team, TeamMembership
from app.repositories.base import BaseRepository

#: Every table whose rows a team can own, with the label used in messages.
OWNED_TABLES: tuple[tuple[str, type], ...] = (
    ("models", Model),
    ("aliases", ModelAlias),
    ("api_clients", ApiClient),
    ("agents", Agent),
    ("knowledge_bases", KnowledgeBase),
)


class TeamRepository(BaseRepository[Team]):
    model = Team

    async def list_teams(self, *, ids: Sequence[uuid.UUID] | None = None) -> Sequence[Team]:
        stmt = select(Team).order_by(Team.name)
        if ids is not None:
            stmt = stmt.where(Team.id.in_(ids))
        return (await self.session.execute(stmt)).scalars().all()

    async def get_by_slug(self, slug: str) -> Team | None:
        return (
            await self.session.execute(select(Team).where(Team.slug == slug))
        ).scalar_one_or_none()

    async def get_by_name(self, name: str) -> Team | None:
        return (
            await self.session.execute(select(Team).where(Team.name == name))
        ).scalar_one_or_none()

    async def team_ids_for(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """The teams a user belongs to — read on every authenticated request."""
        stmt = select(TeamMembership.team_id).where(TeamMembership.user_id == user_id)
        return frozenset((await self.session.execute(stmt)).scalars().all())

    async def members(self, team_id: uuid.UUID) -> Sequence[tuple[TeamMembership, User]]:
        stmt = (
            select(TeamMembership, User)
            .join(User, User.id == TeamMembership.user_id)
            .where(TeamMembership.team_id == team_id)
            .order_by(User.username)
        )
        return [(m, u) for m, u in (await self.session.execute(stmt)).all()]

    async def membership(self, team_id: uuid.UUID, user_id: uuid.UUID) -> TeamMembership | None:
        stmt = select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add_membership(self, membership: TeamMembership) -> None:
        self.session.add(membership)

    async def delete_membership(self, membership: TeamMembership) -> None:
        await self.session.delete(membership)
        await self.session.flush()

    async def member_counts(self) -> dict[uuid.UUID, int]:
        stmt = select(TeamMembership.team_id, func.count()).group_by(TeamMembership.team_id)
        return dict((await self.session.execute(stmt)).tuples().all())

    async def owned_counts(self, team_id: uuid.UUID) -> dict[str, int]:
        """How many resources of each kind this team owns."""
        counts: dict[str, int] = {}
        for label, table in OWNED_TABLES:
            stmt = select(func.count()).select_from(table).where(table.team_id == team_id)  # type: ignore[attr-defined]
            counts[label] = int((await self.session.execute(stmt)).scalar_one())
        return counts

    async def with_memberships(self, team_id: uuid.UUID) -> Team | None:
        stmt = select(Team).where(Team.id == team_id).options(selectinload(Team.memberships))
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ScopeLookupRepository:
    """The owning ``team_id`` of one resource, without loading the resource (E3).

    Each lookup returns ``(exists, team_id)``. A route checks scope *before* calling the
    service, so it must not pay for the full object graph twice; and a row that does not
    exist is left for the service to report, so its 404 keeps its existing wording.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _one(self, stmt: Any) -> tuple[bool, uuid.UUID | None]:
        row = (await self.session.execute(stmt)).first()
        return (False, None) if row is None else (True, row[0])

    async def model(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(select(Model.team_id).where(Model.id == ident))

    async def model_teams(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID | None]:
        stmt = select(Model.id, Model.team_id).where(Model.id.in_(ids))
        return dict((await self.session.execute(stmt)).tuples().all())

    async def deployment(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(
            select(Model.team_id)
            .join(ModelDeployment, ModelDeployment.model_id == Model.id)
            .where(ModelDeployment.id == ident)
        )

    async def alias(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(select(ModelAlias.team_id).where(ModelAlias.id == ident))

    async def api_client(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(select(ApiClient.team_id).where(ApiClient.id == ident))

    async def api_key(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(
            select(ApiClient.team_id)
            .join(ApiKey, ApiKey.client_id == ApiClient.id)
            .where(ApiKey.id == ident)
        )

    async def agent(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(select(Agent.team_id).where(Agent.id == ident))

    async def run(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(
            select(Agent.team_id)
            .join(AgentRun, AgentRun.agent_id == Agent.id)
            .where(AgentRun.id == ident)
        )

    async def run_teams(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID | None]:
        stmt = (
            select(AgentRun.id, Agent.team_id)
            .join(Agent, Agent.id == AgentRun.agent_id)
            .where(AgentRun.id.in_(ids))
        )
        return dict((await self.session.execute(stmt)).tuples().all())

    async def by_public_name(self, name: str) -> list[tuple[bool, uuid.UUID | None]]:
        """The alias of that name (and the model it points at), or the model itself."""
        alias = (
            await self.session.execute(
                select(ModelAlias.team_id, Model.team_id)
                .join(Model, Model.id == ModelAlias.model_id)
                .where(ModelAlias.alias == name)
            )
        ).first()
        if alias is not None:
            return [(True, alias[0]), (True, alias[1])]
        return [await self._one(select(Model.team_id).where(Model.name == name))]

    async def knowledge_base(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(select(KnowledgeBase.team_id).where(KnowledgeBase.id == ident))

    async def document(self, ident: uuid.UUID) -> tuple[bool, uuid.UUID | None]:
        return await self._one(
            select(KnowledgeBase.team_id)
            .join(Document, Document.knowledge_base_id == KnowledgeBase.id)
            .where(Document.id == ident)
        )

    async def team_exists(self, ident: uuid.UUID) -> bool:
        return (await self._one(select(Team.id).where(Team.id == ident)))[0]

    async def set_team(self, table: type, ident: uuid.UUID, team_id: uuid.UUID | None) -> bool:
        stmt = update(table).where(table.id == ident).values(team_id=team_id)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        return bool(result.rowcount)  # type: ignore[attr-defined]
