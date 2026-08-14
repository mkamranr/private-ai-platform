"""Declarative agents, skills and tools (M10-M12).

The same pattern as model and MCP manifests, for the same reason: an air-gapped site
catalogues what it runs by importing files that shipped in the bundle, not by an operator
retyping a system prompt through a form. Two sites given the same bundle then have the
same agents, and the difference between them is configuration rather than typing.

**Import order is tools, then skills, then agents**, and it is not arbitrary: a skill
names the tools it requires and an agent names both, so a later stage resolves names the
earlier one created. Importing agents first would either fail or — worse — succeed with
an empty tool list, producing an agent that answers confidently and can do nothing.

**Re-import updates rather than duplicating.** A manifest is a desired state, and the
operation an operator actually performs is "apply the file again after editing it". For
an agent that means a new *version* (§M14), never an in-place edit: a run records the
version it executed, and rewriting that version would make an old run unexplainable.

**Names, not ids.** Manifests are written by hand and shipped between sites, where ids
differ. Everything cross-references by name, and a reference that cannot be resolved is
reported against the file that made it rather than silently dropped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config.settings import Settings
from app.core.errors import ConflictError
from app.core.logging import get_logger
from app.models.auth import User
from app.repositories.agents import (
    AgentRepository,
    SkillRepository,
    ToolRepository,
)
from app.services.agent_registry import (
    AgentRegistryService,
    SkillRegistryService,
    ToolRegistryService,
)

log = get_logger(__name__)


@dataclass(slots=True)
class DefinitionResult:
    """What happened to one manifest. Reported per file, so a partial import says which
    file failed rather than only that something did."""

    kind: str
    name: str
    action: str  # created | updated | unchanged | skipped
    detail: str | None = None


class DefinitionImporter:
    def __init__(
        self,
        settings: Settings,
        tools_repo: ToolRepository,
        skills_repo: SkillRepository,
        agents_repo: AgentRepository,
        tools: ToolRegistryService,
        skills: SkillRegistryService,
        agents: AgentRegistryService,
    ) -> None:
        self._settings = settings
        self._tools_repo = tools_repo
        self._skills_repo = skills_repo
        self._agents_repo = agents_repo
        self._tools = tools
        self._skills = skills
        self._agents = agents

    async def import_all(self, *, actor: User) -> list[DefinitionResult]:
        results: list[DefinitionResult] = []
        results += await self._import_tools(actor=actor)
        results += await self._import_skills()
        results += await self._import_agents(actor=actor)
        return results

    # -- tools -------------------------------------------------------------
    async def _import_tools(self, *, actor: User) -> list[DefinitionResult]:
        results: list[DefinitionResult] = []
        for filename, spec in await _read(self._settings.agents.tool_manifest_path):
            name = str(spec.get("name") or "").strip()
            if not name:
                results.append(DefinitionResult("tool", filename, "skipped", "no name"))
                continue

            existing = await self._tools_repo.get_by_name(name)
            if existing is not None:
                # Not updated in place. A tool's type and parameter schema define what it
                # *is*, and changing them under an agent already granted it would silently
                # change what that agent can do (see docs/tools.md).
                results.append(DefinitionResult("tool", name, "unchanged", "already registered"))
                continue

            try:
                await self._tools.register_tool(
                    name=name,
                    display_name=str(spec.get("display_name") or name),
                    description=str(spec.get("description") or ""),
                    tool_type=str(spec.get("type") or "INTERNAL"),
                    parameters_schema=dict(spec.get("parameters_schema") or {}),
                    required_permission=str(spec.get("required_permission") or ""),
                    risk_level=str(spec.get("risk_level") or "LOW"),
                    endpoint=spec.get("endpoint"),
                    config=dict(spec.get("config") or {}),
                    # Never from a manifest. A credential in a file that ships between
                    # sites is a credential every site shares; they are set through the
                    # API, encrypted at rest, after the tool exists.
                    credentials=None,
                    timeout_seconds=int(spec.get("timeout_seconds") or 30),
                    actor=actor,
                )
            except Exception as exc:
                results.append(DefinitionResult("tool", name, "skipped", _reason(exc)))
                continue
            results.append(DefinitionResult("tool", name, "created"))
        return results

    # -- skills ------------------------------------------------------------
    async def _import_skills(self) -> list[DefinitionResult]:
        results: list[DefinitionResult] = []
        for filename, spec in await _read(self._settings.agents.skill_manifest_path):
            name = str(spec.get("name") or "").strip()
            if not name:
                results.append(DefinitionResult("skill", filename, "skipped", "no name"))
                continue

            required_tools = [str(t) for t in spec.get("required_tools") or []]
            missing = [t for t in required_tools if await self._tools_repo.get_by_name(t) is None]
            if missing:
                # Reported, not fatal. A skill may legitimately require a tool that is
                # discovered from an MCP server this site has not started — the skill is
                # still worth having, and the pipeline refuses the call at run time.
                log.info("skill_requires_unregistered_tools", skill=name, tools=missing)

            existing = await self._skills_repo.get_by_name(name)
            if existing is not None:
                # Instructions and requirements are the whole content of a skill, so
                # re-import applies them. Skills are not versioned per run the way agents
                # are: an agent version records the skill *names* it used, and the text a
                # run actually saw is in its trace.
                existing.display_name = str(spec.get("display_name") or existing.display_name)
                existing.description = str(spec.get("description") or existing.description)
                existing.instructions = str(spec.get("instructions") or existing.instructions)
                existing.version = str(spec.get("version") or existing.version)
                existing.required_tools = required_tools
                existing.required_knowledge = [str(k) for k in spec.get("required_knowledge") or []]
                await self._skills_repo.flush()
                results.append(DefinitionResult("skill", name, "updated"))
                continue

            await self._skills.create_skill(
                name=name,
                display_name=str(spec.get("display_name") or name),
                description=str(spec.get("description") or ""),
                instructions=str(spec.get("instructions") or ""),
                version=str(spec.get("version") or "1.0"),
                required_tools=required_tools,
                required_knowledge=[str(k) for k in spec.get("required_knowledge") or []],
                parameters=dict(spec.get("parameters") or {}),
                required_permission=spec.get("required_permission"),
            )
            results.append(DefinitionResult("skill", name, "created"))
        return results

    # -- agents ------------------------------------------------------------
    async def _import_agents(self, *, actor: User) -> list[DefinitionResult]:
        results: list[DefinitionResult] = []
        for filename, spec in await _read(self._settings.agents.agent_manifest_path):
            slug = str(spec.get("slug") or "").strip()
            if not slug:
                results.append(DefinitionResult("agent", filename, "skipped", "no slug"))
                continue

            tool_ids, unknown_tools = await self._resolve_tools(spec.get("tools") or [])
            skill_ids, unknown_skills = await self._resolve_skills(spec.get("skills") or [])
            unresolved = unknown_tools + unknown_skills

            existing = await self._agents_repo.get_by_slug(slug)
            try:
                if existing is None:
                    await self._agents.create_agent(
                        slug=slug,
                        display_name=str(spec.get("display_name") or slug),
                        description=spec.get("description"),
                        system_prompt=str(spec.get("system_prompt") or ""),
                        model=str(spec.get("model") or "enterprise-chat"),
                        temperature=float(
                            spec.get("temperature", self._settings.agents.default_temperature)
                        ),
                        max_iterations=int(spec.get("max_iterations") or 10),
                        tool_ids=tool_ids,
                        skill_ids=skill_ids,
                        knowledge_base_ids=[str(k) for k in spec.get("knowledge_bases") or []],
                        memory_enabled=bool(spec.get("memory_enabled") or False),
                        actor=actor,
                    )
                    action = "created"
                else:
                    # A new version, never an edit. A run records the version it executed,
                    # and rewriting that version in place would make an old run
                    # unexplainable (§M14).
                    await self._agents.create_version(
                        existing.id,
                        system_prompt=str(spec.get("system_prompt") or ""),
                        model=str(spec.get("model") or "enterprise-chat"),
                        temperature=float(
                            spec.get("temperature", self._settings.agents.default_temperature)
                        ),
                        max_iterations=int(spec.get("max_iterations") or 10),
                        tool_ids=tool_ids,
                        skill_ids=skill_ids,
                        knowledge_base_ids=[str(k) for k in spec.get("knowledge_bases") or []],
                        memory_enabled=bool(spec.get("memory_enabled") or False),
                        change_note=f"Imported from {filename}.",
                        actor=actor,
                    )
                    action = "updated"
            except Exception as exc:
                results.append(DefinitionResult("agent", slug, "skipped", _reason(exc)))
                continue

            detail = (
                f"references not registered here: {', '.join(unresolved)}" if unresolved else None
            )
            results.append(DefinitionResult("agent", slug, action, detail))
        return results

    async def _resolve_tools(self, names: list[Any]) -> tuple[list[Any], list[str]]:
        """Names to ids, reporting what could not be found.

        Unresolved names do not stop the import: the commonest case is a tool discovered
        from an MCP server that this site has not deployed, and an agent that is otherwise
        correct should still exist. What must not happen is silence — the caller is told,
        because an agent granted three tools and given one is not the agent the manifest
        describes.
        """
        ids: list[Any] = []
        unknown: list[str] = []
        for raw in names:
            name = str(raw)
            tool = await self._tools_repo.get_by_name(name)
            if tool is None:
                unknown.append(f"tool {name}")
                continue
            ids.append(tool.id)
        return ids, unknown

    async def _resolve_skills(self, names: list[Any]) -> tuple[list[Any], list[str]]:
        ids: list[Any] = []
        unknown: list[str] = []
        for raw in names:
            name = str(raw)
            skill = await self._skills_repo.get_by_name(name)
            if skill is None:
                unknown.append(f"skill {name}")
                continue
            ids.append(skill.id)
        return ids, unknown


def _reason(exc: Exception) -> str:
    if isinstance(exc, ConflictError):
        return "already exists"
    return f"{type(exc).__name__}: {exc}"[:200]


async def _read(directory: str) -> list[tuple[str, dict]]:
    """Read every manifest in a directory as (filename, spec).

    Blocking file I/O, so it runs in a thread — the same treatment model and MCP
    manifests get. An absent directory is normal (a site may ship no agents of its own)
    and yields nothing rather than an error.
    """

    def _load() -> list[tuple[str, dict]]:
        path = Path(directory)
        if not path.is_dir():
            return []
        out: list[tuple[str, dict]] = []
        for file in sorted(path.glob("*.y*ml")):
            try:
                spec = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                log.warning("definition_unreadable", file=file.name, error=str(exc)[:200])
                continue
            if isinstance(spec, dict):
                out.append((file.name, spec))
        return out

    return await asyncio.to_thread(_load)
