"""Team scope: which teams' resources a caller may see (expansion E3, plan §5.12).

The rule, applied identically to every scoped resource:

* ``team_id IS NULL`` — **shared**: visible to anyone holding the resource's permission.
  Everything created before teams existed is shared, so nothing changed on upgrade.
* ``team_id = T`` — visible only to members of ``T``, and to callers whose scope covers
  every team (superusers, holders of ``scope.all``).

Permissions still decide *what* a caller may do; scope decides *to which rows*. Both must
pass. An out-of-scope resource is reported as **not found**, never as forbidden: a 403
would confirm that a resource with that id exists in some other team.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, or_

from app.core.errors import NotFoundError, PermissionDeniedError


@dataclass(frozen=True, slots=True)
class Scope:
    all_teams: bool = False
    team_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)

    @classmethod
    def everything(cls) -> Scope:
        """For trusted internal callers (workers), which act on behalf of the platform."""
        return cls(all_teams=True)

    @classmethod
    def for_team(cls, team_id: uuid.UUID | None) -> Scope:
        """The scope of an API key: its client's team, or shared resources only."""
        return cls(team_ids=frozenset({team_id}) if team_id else frozenset())

    def can_see(self, team_id: uuid.UUID | None) -> bool:
        return team_id is None or self.all_teams or team_id in self.team_ids

    def require_visible(self, team_id: uuid.UUID | None, *, what: str, ident: Any) -> None:
        """Raise the same 404 a missing resource would."""
        if not self.can_see(team_id):
            raise NotFoundError(f"No {what} with id {ident}.")

    def require_assignable(self, team_id: uuid.UUID | None) -> None:
        """May the caller put a resource into ``team_id``?

        Into a team: only a member of it (or an all-teams caller). Into shared (None):
        anyone who may create the resource at all, as before teams existed.
        """
        if team_id is not None and not (self.all_teams or team_id in self.team_ids):
            raise PermissionDeniedError(
                "You can only assign resources to a team you are a member of.",
                details={"field": "team_id", "team_id": str(team_id)},
            )

    def clause(self, column: Any) -> ColumnElement[bool] | None:
        """A WHERE condition limiting ``column`` (a ``team_id``) to this scope, or None
        when nothing needs filtering."""
        if self.all_teams:
            return None
        if not self.team_ids:
            shared: ColumnElement[bool] = column.is_(None)
            return shared
        return or_(column.is_(None), column.in_(self.team_ids))

    def filter(self, items: Iterable[Any], *, attr: str = "team_id") -> list[Any]:
        """Keep the items this scope can see — for lists that are not paginated."""
        return [item for item in items if self.can_see(getattr(item, attr))]
