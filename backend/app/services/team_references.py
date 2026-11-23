"""What an agent or workflow may use, judged by its own team (expansion E18).

E3 checked an agent's model and knowledge bases against the **person saving it**. That
left two holes. A shared agent built by a member of team A could name team A's model and
so offer it to everyone. And nothing was checked again, so an agent kept using a model
after the model moved to another team.

The rule here is the one API keys already follow: a resource may use what its **own
team** can see, meaning shared resources plus its team's own (`Scope.for_team`). It is
applied when an agent or workflow is saved, when it is moved to another team, and every
time it runs. A shared agent or workflow may therefore use only shared models and
knowledge bases. Since E35 an agent's tools are checked the same way.

An unknown name or id passes. Existence is the owning service's to report, in its own
words, and a template such as ``{{ input.model }}`` can only be judged at run time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.core.scope import Scope
from app.repositories.teams import ScopeLookupRepository


class TeamReferences:
    def __init__(self, lookups: ScopeLookupRepository) -> None:
        self._lookups = lookups

    async def unavailable(
        self,
        team_id: uuid.UUID | None,
        *,
        models: Iterable[str] = (),
        knowledge_bases: Iterable[str] = (),
        tools: Iterable[uuid.UUID | str] = (),
        workflows: Iterable[uuid.UUID | str] = (),
    ) -> list[str]:
        """The references ``team_id`` may not use, described for a person, or ``[]``."""
        scope = Scope.for_team(team_id)
        problems: list[str] = []
        for name in models:
            if not name or "{{" in name:
                continue
            for exists, owner in await self._lookups.by_public_name(name):
                if exists and not scope.can_see(owner):
                    problems.append(f"model {name!r}")
                    break
        for ref in knowledge_bases:
            try:
                ident = uuid.UUID(str(ref))
            except ValueError:
                continue
            exists, owner = await self._lookups.knowledge_base(ident)
            if exists and not scope.can_see(owner):
                problems.append(f"knowledge base {ref}")
        # E35: tools granted to an agent, by id.
        tool_ids: set[uuid.UUID] = set()
        for tool_ref in tools:
            try:
                tool_ids.add(
                    tool_ref if isinstance(tool_ref, uuid.UUID) else uuid.UUID(str(tool_ref))
                )
            except ValueError:
                continue
        found = await self._lookups.tool_teams(tool_ids)
        for _ident, (name, owner) in sorted(found.items(), key=lambda kv: kv[1][0]):
            if not scope.can_see(owner):
                problems.append(f"tool {name!r}")
        # E71: workflows run by a workflow's sub-workflow and foreach steps.
        workflow_ids: set[uuid.UUID] = set()
        for workflow_ref in workflows:
            try:
                workflow_ids.add(uuid.UUID(str(workflow_ref)))
            except ValueError:
                continue
        if workflow_ids:
            teams = await self._lookups.workflow_teams(workflow_ids)
            for ident in sorted(teams, key=str):
                if not scope.can_see(teams[ident]):
                    problems.append(f"workflow {ident}")
        return problems


def describe(what: str, team_id: uuid.UUID | None, problems: list[str]) -> str:
    """One sentence naming what is out of reach and how to fix it."""
    owner = "its team" if team_id else "shared resources (it is shared)"
    return (
        f"This {what} uses {', '.join(problems)}, which belongs to another team. "
        f"It may only use {owner}. Move the {what} into the same team, or make the "
        "resource shared."
    )
