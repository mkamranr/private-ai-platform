"""Teams, memberships and resource scope (expansion E3, plan §5.12).

Two services:

* :class:`TeamService` — create teams, manage who is in them.
* :class:`ScopeGuard` — answer "may this caller see resource X?" for one request, and
  move a resource between teams. Routes call it *before* the resource's own service, so
  every existing service keeps working unchanged for internal callers (workers, the
  CLI), which act on behalf of the platform and are not team-scoped.

The visibility rule itself is in :mod:`app.core.scope`.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.scope import Scope
from app.models.agents import Agent
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.knowledge import KnowledgeBase
from app.models.models_registry import ApiClient, Model, ModelAlias
from app.models.teams import Team, TeamMembership, TeamRole
from app.models.workflows import Workflow
from app.repositories.teams import ScopeLookupRepository, TeamRepository
from app.repositories.user import UserRepository
from app.services.audit import AuditService
from app.services.auth import AuthService

log = get_logger(__name__)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Resource kinds that can be moved between teams, with the permission that moving one
#: requires — the same permission that lets a caller change that resource at all.
ASSIGNABLE: dict[str, tuple[type, str, str]] = {
    "model": (Model, Permission.MODEL_REGISTER, "model"),
    "alias": (ModelAlias, Permission.MODEL_DEPLOY, "alias"),
    "api_client": (ApiClient, Permission.APIKEY_MANAGE, "API client"),
    "agent": (Agent, Permission.AGENT_EDIT, "agent"),
    "knowledge_base": (KnowledgeBase, Permission.KNOWLEDGE_MANAGE, "knowledge base"),
    "workflow": (Workflow, Permission.WORKFLOW_EDIT, "workflow"),
}


def scope_for(user: User, team_ids: frozenset[uuid.UUID]) -> Scope:
    """A user's scope: every team for a superuser or a holder of ``scope.all``."""
    return Scope(
        all_teams=AuthService.has_permission(user, Permission.SCOPE_ALL), team_ids=team_ids
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64] or "team"


class TeamService:
    def __init__(self, teams: TeamRepository, users: UserRepository, audit: AuditService) -> None:
        self._teams = teams
        self._users = users
        self._audit = audit

    # -- reads -------------------------------------------------------------
    async def team_ids_for(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return await self._teams.team_ids_for(user_id)

    async def list_teams(self, scope: Scope) -> list[dict[str, Any]]:
        """Every team for an all-teams caller; otherwise the caller's own."""
        teams = await self._teams.list_teams(ids=None if scope.all_teams else list(scope.team_ids))
        counts = await self._teams.member_counts()
        return [self.describe(t, member_count=counts.get(t.id, 0)) for t in teams]

    async def get(self, team_id: uuid.UUID, scope: Scope) -> Team:
        team = await self._teams.get(team_id)
        # Someone else's team is not found, exactly like someone else's model.
        if team is None or not (scope.all_teams or team.id in scope.team_ids):
            raise NotFoundError(f"No team with id {team_id}.")
        return team

    async def detail(self, team_id: uuid.UUID, scope: Scope) -> dict[str, Any]:
        team = await self.get(team_id, scope)
        members = await self.members(team.id)
        out = self.describe(team, member_count=len(members))
        out["members"] = members
        out["resources"] = await self._teams.owned_counts(team.id)
        return out

    async def members(self, team_id: uuid.UUID) -> list[dict[str, Any]]:
        return [
            {
                "user_id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "role": membership.role,
                "added_at": membership.created_at,
            }
            for membership, user in await self._teams.members(team_id)
        ]

    # -- teams -------------------------------------------------------------
    async def create(
        self, *, name: str, slug: str | None, description: str | None, actor: User
    ) -> Team:
        name = name.strip()
        slug = (slug or slugify(name)).strip()
        if not _SLUG.match(slug):
            raise ValidationError(
                "A slug is lowercase letters, digits and hyphens, starting with a letter or digit.",
                details={"field": "slug"},
            )
        if await self._teams.get_by_name(name) is not None:
            raise ConflictError(f"A team named {name!r} already exists.", details={"field": "name"})
        if await self._teams.get_by_slug(slug) is not None:
            raise ConflictError(f"The slug {slug!r} is taken.", details={"field": "slug"})

        team = Team(name=name, slug=slug, description=description, created_by=actor.id)
        self._teams.add(team)
        await self._teams.flush()
        await self._teams.session.refresh(team)
        await self._audit.record(
            AuditAction.TEAM_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team.id),
            metadata={"name": name, "slug": slug},
        )
        log.info("team_created", slug=slug)
        return team

    async def update(
        self, team_id: uuid.UUID, changes: dict[str, Any], *, scope: Scope, actor: User
    ) -> Team:
        team = await self.get(team_id, scope)
        before = {"name": team.name, "description": team.description}
        if changes.get("name") is not None:
            name = changes["name"].strip()
            other = await self._teams.get_by_name(name)
            if other is not None and other.id != team.id:
                raise ConflictError(
                    f"A team named {name!r} already exists.", details={"field": "name"}
                )
            team.name = name
        if "description" in changes:
            team.description = changes["description"]
        await self._teams.flush()
        await self._teams.session.refresh(team)
        await self._audit.record(
            AuditAction.TEAM_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team.id),
            metadata={
                "before": before,
                "after": {"name": team.name, "description": team.description},
            },
        )
        return team

    async def delete(self, team_id: uuid.UUID, *, scope: Scope, actor: User) -> None:
        """Refused while the team owns anything.

        Deleting it would otherwise either cascade (destroying models and keys) or make
        everything it owned shared with the whole platform — the opposite of what a team
        was for. Move or delete the resources first; the error says which.
        """
        team = await self.get(team_id, scope)
        owned = {k: v for k, v in (await self._teams.owned_counts(team.id)).items() if v}
        if owned:
            raise ConflictError(
                f"Team {team.name!r} still owns resources. Move them to another team (or to "
                "shared) or delete them first.",
                details={"resources": owned},
            )
        snapshot = {"name": team.name, "slug": team.slug}
        await self._teams.delete(team)
        await self._teams.flush()
        await self._audit.record(
            AuditAction.TEAM_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team_id),
            metadata=snapshot,
        )

    # -- membership --------------------------------------------------------
    async def require_member_manager(self, team_id: uuid.UUID, actor: User, scope: Scope) -> Team:
        """Platform-wide ``team.manage`` or an owner of this team."""
        team = await self.get(team_id, scope)
        if AuthService.has_permission(actor, Permission.TEAM_MANAGE):
            return team
        membership = await self._teams.membership(team.id, actor.id)
        if membership is None or membership.role != TeamRole.OWNER:
            raise PermissionDeniedError(
                "Managing this team's members requires team.manage or being a team owner."
            )
        return team

    async def add_member(
        self, team_id: uuid.UUID, *, user_id: uuid.UUID, role: str, scope: Scope, actor: User
    ) -> dict[str, Any]:
        team = await self.require_member_manager(team_id, actor, scope)
        self._check_role(role)
        user = await self._users.get(user_id)
        if user is None:
            raise ValidationError(f"No user with id {user_id}.", details={"field": "user_id"})
        if await self._teams.membership(team.id, user_id) is not None:
            raise ConflictError(
                f"{user.username} is already a member of {team.name}.",
                details={"field": "user_id"},
            )
        self._teams.add_membership(TeamMembership(team_id=team.id, user_id=user_id, role=role))
        try:
            await self._teams.flush()
        except IntegrityError as exc:  # a concurrent add of the same member
            raise ConflictError(f"{user.username} is already a member of {team.name}.") from exc
        await self._audit.record(
            AuditAction.TEAM_MEMBER_ADDED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team.id),
            metadata={"member": user.username, "member_id": str(user_id), "role": role},
        )
        return {"user_id": user.id, "username": user.username, "role": role}

    async def update_member(
        self, team_id: uuid.UUID, user_id: uuid.UUID, *, role: str, scope: Scope, actor: User
    ) -> dict[str, Any]:
        team = await self.require_member_manager(team_id, actor, scope)
        self._check_role(role)
        membership = await self._membership_or_404(team, user_id)
        await self._keep_an_owner(team, membership, new_role=role)
        before = membership.role
        membership.role = role
        await self._teams.flush()
        await self._audit.record(
            AuditAction.TEAM_MEMBER_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team.id),
            metadata={"member_id": str(user_id), "before": before, "after": role},
        )
        return {"user_id": user_id, "role": role}

    async def remove_member(
        self, team_id: uuid.UUID, user_id: uuid.UUID, *, scope: Scope, actor: User
    ) -> None:
        team = await self.require_member_manager(team_id, actor, scope)
        membership = await self._membership_or_404(team, user_id)
        await self._keep_an_owner(team, membership, new_role=None)
        await self._teams.delete_membership(membership)
        await self._audit.record(
            AuditAction.TEAM_MEMBER_REMOVED,
            user_id=actor.id,
            username=actor.username,
            resource_type="team",
            resource_id=str(team.id),
            metadata={"member_id": str(user_id)},
        )

    # -- helpers -----------------------------------------------------------
    async def _membership_or_404(self, team: Team, user_id: uuid.UUID) -> TeamMembership:
        membership = await self._teams.membership(team.id, user_id)
        if membership is None:
            raise NotFoundError(f"That user is not a member of {team.name}.")
        return membership

    async def _keep_an_owner(
        self, team: Team, membership: TeamMembership, *, new_role: str | None
    ) -> None:
        """A team that has owners must keep at least one.

        Otherwise an owner can demote themselves and leave the team manageable only by a
        platform administrator. A team created without owners is fine — `team.manage`
        holders run it.
        """
        if membership.role != TeamRole.OWNER or new_role == TeamRole.OWNER:
            return
        owners = [m for m, _ in await self._teams.members(team.id) if m.role == TeamRole.OWNER]
        if len(owners) <= 1:
            raise ConflictError(
                f"That is the last owner of {team.name}. Make someone else an owner first."
            )

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in set(TeamRole):
            raise ValidationError(
                f"Unknown team role {role!r}.",
                details={"field": "role", "allowed": sorted(TeamRole)},
            )

    @staticmethod
    def describe(team: Team, *, member_count: int) -> dict[str, Any]:
        return {
            "id": team.id,
            "name": team.name,
            "slug": team.slug,
            "description": team.description,
            "tenant_id": team.tenant_id,
            "member_count": member_count,
            "created_at": team.created_at,
        }


class ScopeGuard:
    """Per-request scope checks over the lightweight team lookups."""

    def __init__(self, scope: Scope, lookups: ScopeLookupRepository, audit: AuditService) -> None:
        self.scope = scope
        self._lookups = lookups
        self._audit = audit

    async def _check(self, found: tuple[bool, uuid.UUID | None], what: str, ident: Any) -> None:
        exists, team_id = found
        if exists:
            self.scope.require_visible(team_id, what=what, ident=ident)

    # Each raises the resource's ordinary 404 when it is out of scope; a resource that does
    # not exist at all passes through, so its service reports it in its own words.
    async def model(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.model(ident), "model", ident)

    async def visible_models(self, model_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        """Which of these models the caller can see — for filtering lists of things that
        belong to a model (deployments)."""
        if self.scope.all_teams or not model_ids:
            return set(model_ids)
        teams = await self._lookups.model_teams(model_ids)
        return {mid for mid in model_ids if self.scope.can_see(teams.get(mid))}

    async def deployment(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.deployment(ident), "deployment", ident)

    async def alias(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.alias(ident), "alias", ident)

    async def api_client(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.api_client(ident), "API client", ident)

    async def api_key(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.api_key(ident), "API key", ident)

    async def agent(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.agent(ident), "agent", ident)

    async def run(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.run(ident), "run", ident)

    async def visible_runs(self, run_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        if self.scope.all_teams or not run_ids:
            return set(run_ids)
        teams = await self._lookups.run_teams(run_ids)
        return {rid for rid in run_ids if self.scope.can_see(teams.get(rid))}

    # -- workflows (E6): a run and an approval are in scope when their workflow is.
    async def workflow(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.workflow(ident), "workflow", ident)

    async def workflow_run(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.workflow_run(ident), "workflow run", ident)

    async def workflow_approval(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.workflow_approval(ident), "approval", ident)

    async def visible_workflows(self, workflow_ids: set[uuid.UUID]) -> set[uuid.UUID]:
        if self.scope.all_teams or not workflow_ids:
            return set(workflow_ids)
        teams = await self._lookups.workflow_teams(workflow_ids)
        return {wid for wid in workflow_ids if self.scope.can_see(teams.get(wid))}

    async def model_name(self, name: str) -> None:
        """An alias or model *name* (what agents and knowledge bases refer to) must be
        visible. An unknown name passes: the owning service validates existence."""
        for exists, team_id in await self._lookups.by_public_name(name):
            if exists and not self.scope.can_see(team_id):
                raise NotFoundError(f"No model or alias named {name!r}.")

    async def knowledge_base_refs(self, refs: list[str]) -> None:
        for ref in refs:
            try:
                ident = uuid.UUID(ref)
            except ValueError:
                continue  # malformed ids are the registry's to reject
            await self.knowledge_base(ident)

    async def knowledge_base(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.knowledge_base(ident), "knowledge base", ident)

    async def document(self, ident: uuid.UUID) -> None:
        await self._check(await self._lookups.document(ident), "document", ident)

    async def assignable(self, team_id: uuid.UUID | None) -> None:
        """Validate a ``team_id`` from a create payload."""
        if team_id is not None and not await self._lookups.team_exists(team_id):
            raise ValidationError(f"No team with id {team_id}.", details={"field": "team_id"})
        self.scope.require_assignable(team_id)

    async def place(self, obj: Any, team_id: uuid.UUID | None) -> None:
        """Put a just-created resource into ``team_id`` (already checked by assignable)."""
        if team_id is not None:
            obj.team_id = team_id
            await self._lookups.session.flush()

    async def reassign(
        self,
        *,
        resource_type: str,
        resource_id: uuid.UUID,
        team_id: uuid.UUID | None,
        actor: User,
        auth: AuthService,
    ) -> dict[str, Any]:
        """Move a resource to ``team_id`` (None = shared)."""
        if resource_type not in ASSIGNABLE:
            raise ValidationError(
                f"Unknown resource type {resource_type!r}.",
                details={"field": "resource_type", "allowed": sorted(ASSIGNABLE)},
            )
        table, permission, label = ASSIGNABLE[resource_type]
        await auth.require_permission(actor, permission)

        lookup = getattr(self._lookups, resource_type)
        exists, current = await lookup(resource_id)
        if not exists or not self.scope.can_see(current):
            raise NotFoundError(f"No {label} with id {resource_id}.")
        await self.assignable(team_id)
        # Taking a resource *out* of a team is a move too: only someone who could have
        # put it there may do it, or a team could be emptied by a non-member.
        self.scope.require_assignable(current)

        await self._lookups.set_team(table, resource_id, team_id)
        await self._audit.record(
            AuditAction.SCOPE_ASSIGNED,
            user_id=actor.id,
            username=actor.username,
            resource_type=resource_type,
            resource_id=str(resource_id),
            metadata={
                "before": str(current) if current else None,
                "after": str(team_id) if team_id else None,
            },
        )
        return {
            "resource_type": resource_type,
            "resource_id": resource_id,
            "team_id": team_id,
            "previous_team_id": current,
        }
