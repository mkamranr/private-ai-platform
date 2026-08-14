"""Agent, skill, tool and MCP registries (M10-M13).

The registries are deliberately dull. All the interesting behaviour lives in the §10
pipeline and the runtime; what happens here is bookkeeping with two properties worth
defending:

**An edit creates a version, it never mutates one.** ``AgentVersion`` rows are immutable
after creation. That is what lets a run from three months ago still be explained.

**Discovery populates, it never grants.** Finding a tool on an MCP server catalogues it
and stops. Enabling it, and assigning it to an agent, are separate deliberate acts —
otherwise pointing the platform at a server would silently widen what every agent can do.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.config.settings import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.interfaces.agent import AgentSpec
from app.core.interfaces.tools import RiskLevel, ToolDefinition, ToolType
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.agents import (
    Agent,
    AgentSkill,
    AgentTool,
    AgentVersion,
    McpServer,
    Skill,
    Tool,
)
from app.models.audit import AuditAction
from app.models.auth import User
from app.repositories.agents import (
    AgentRepository,
    AgentVersionRepository,
    McpServerRepository,
    SkillRepository,
    ToolRepository,
)
from app.schemas.agents import AgentVersionRead
from app.services.audit import AuditService
from app.services.tool_executors import McpError, call_mcp
from app.services.tool_pipeline import _definition

log = get_logger(__name__)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

#: Risk level assigned to a freshly discovered tool.
#:
#: HIGH, not LOW. Nobody has read what a discovered tool does, and the safe failure is an
#: approval prompt an operator can lower after review — not an agent quietly calling
#: something nobody assessed. Lowering it is a deliberate act with an audit record.
DISCOVERED_RISK_LEVEL = RiskLevel.HIGH


@dataclass(slots=True)
class DiscoveryResult:
    server_name: str
    found: int
    created: int
    updated: int
    detail: str | None = None


class ToolRegistryService:
    def __init__(
        self,
        settings: Settings,
        tools: ToolRepository,
        audit: AuditService,
        cipher: SecretCipher,
        # Mapping, not dict: it is only ever read from (one `.get` below), and `dict` is
        # invariant — so a caller holding the executor table's real type,
        # dict[ToolType, ToolExecutor], could not pass it without a cast.
        executors: Mapping[ToolType, object],
    ) -> None:
        self._settings = settings
        self._tools = tools
        self._audit = audit
        self._cipher = cipher
        self._executors = executors

    async def list_tools(self) -> list[Tool]:
        return list(await self._tools.list_all())

    async def get_tool(self, tool_id: uuid.UUID) -> Tool:
        tool = await self._tools.get(tool_id)
        if tool is None:
            raise NotFoundError(f"No tool with id {tool_id}.")
        return tool

    async def register_tool(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
        tool_type: str,
        parameters_schema: dict,
        required_permission: str,
        risk_level: str,
        endpoint: str | None,
        config: dict,
        credentials: str | None,
        timeout_seconds: int,
        actor: User,
    ) -> Tool:
        if await self._tools.get_by_name(name):
            raise ConflictError(f"A tool named {name!r} already exists.")
        if tool_type not in set(ToolType):
            raise ValidationError(
                f"Unknown tool type {tool_type!r}.",
                details={"allowed": sorted(str(t) for t in ToolType)},
            )

        # Registerable, never runnable. Catalogued so an operator can see what exists and
        # plan for it, refused at execution by the pipeline (§25 over §M12).
        detail = None
        if tool_type in self._settings.agents.disabled_tool_types:
            detail = (
                f"{tool_type} tools are disabled on this platform and will be refused at "
                "execution time."
            )

        tool = Tool(
            name=name,
            display_name=display_name,
            description=description,
            type=tool_type,
            parameters_schema=parameters_schema,
            required_permission=required_permission,
            risk_level=risk_level,
            endpoint=endpoint,
            config=config,
            credentials_encrypted=self._cipher.encrypt(credentials) if credentials else None,
            timeout_seconds=timeout_seconds,
        )
        self._tools.add(tool)
        await self._tools.flush()

        await self._audit.record(
            AuditAction.TOOL_ENABLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="tool",
            resource_id=str(tool.id),
            message=detail,
            metadata={
                "name": name,
                "type": tool_type,
                "risk_level": risk_level,
                "required_permission": required_permission,
            },
        )
        return tool

    async def test_tool(self, tool_id: uuid.UUID) -> str:
        """Validate a tool's definition against its executor (§M12).

        Exists so an operator finds a broken endpoint or credential at registration time
        rather than mid-conversation, three tool calls into an agent run.
        """
        tool = await self.get_tool(tool_id)
        executor = self._executors.get(ToolType(tool.type))
        if executor is None:
            raise ValidationError(f"No executor is available for {tool.type} tools.")
        try:
            await executor.validate(self._with_credentials(tool))  # type: ignore[attr-defined]
        except Exception as exc:
            raise ValidationError(f"{tool.name} is not usable: {exc}") from exc
        return f"{tool.name} validated against its {tool.type} executor."

    async def update_tool(self, tool_id: uuid.UUID, payload: object, *, actor: User) -> Tool:
        """Apply an operator's changes.

        Type and parameter schema are deliberately not updatable — they define what the
        tool *is*, and changing them under an agent already granted it would silently
        change what that agent can do. Register a new tool instead.
        """
        tool = await self.get_tool(tool_id)
        changed: list[str] = []

        for field in (
            "display_name",
            "description",
            "required_permission",
            "risk_level",
            "endpoint",
            "config",
            "enabled",
            "timeout_seconds",
        ):
            value = getattr(payload, field, None)
            if value is not None and value != getattr(tool, field):
                setattr(tool, field, value)
                changed.append(field)

        credentials = getattr(payload, "credentials", None)
        if credentials:
            tool.credentials_encrypted = self._cipher.encrypt(credentials)
            changed.append("credentials")

        await self._tools.flush()
        await self._audit.record(
            AuditAction.TOOL_ENABLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="tool",
            resource_id=str(tool.id),
            message=f"Updated {', '.join(changed)}." if changed else "No change.",
            # Risk level and permission are the security-relevant fields; recorded by name
            # and value so a later audit can see the moment a tool was made easier to call.
            metadata={
                "name": tool.name,
                "changed": changed,
                "risk_level": tool.risk_level,
                "required_permission": tool.required_permission,
            },
        )
        return tool

    async def delete_tool(self, tool_id: uuid.UUID, *, actor: User) -> None:
        tool = await self.get_tool(tool_id)
        await self._tools.delete(tool)
        await self._audit.record(
            AuditAction.TOOL_ENABLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="tool",
            resource_id=str(tool_id),
            message=f"Deleted tool {tool.name}.",
        )

    def _with_credentials(self, tool: Tool) -> ToolDefinition:
        """Project for an executor, smuggling the encrypted credential through config.

        The executor decrypts at the last moment. Passing the plaintext here would put it
        in a dataclass that gets logged, traced and serialised into events.
        """
        definition = _definition(tool)
        if tool.credentials_encrypted:
            definition.config["_credentials_encrypted"] = tool.credentials_encrypted
        return definition


class McpRegistryService:
    def __init__(
        self,
        settings: Settings,
        servers: McpServerRepository,
        tools: ToolRepository,
        audit: AuditService,
        cipher: SecretCipher,
    ) -> None:
        self._settings = settings
        self._servers = servers
        self._tools = tools
        self._audit = audit
        self._cipher = cipher

    async def list_servers(self) -> list[McpServer]:
        return list(await self._servers.list_all())

    async def get_server(self, server_id: uuid.UUID) -> McpServer:
        server = await self._servers.get(server_id)
        if server is None:
            raise NotFoundError(f"No MCP server with id {server_id}.")
        return server

    async def register_server(
        self,
        *,
        name: str,
        endpoint: str,
        description: str | None,
        transport: str,
        credentials: str | None,
        actor: User,
    ) -> McpServer:
        if await self._servers.get_by_name(name):
            raise ConflictError(f"An MCP server named {name!r} already exists.")

        server = McpServer(
            name=name,
            endpoint=endpoint,
            description=description,
            transport=transport,
            credentials_encrypted=self._cipher.encrypt(credentials) if credentials else None,
        )
        self._servers.add(server)
        await self._servers.flush()

        await self._audit.record(
            AuditAction.MCP_SERVER_REGISTERED,
            user_id=actor.id,
            username=actor.username,
            resource_type="mcp_server",
            resource_id=str(server.id),
            metadata={"name": name, "endpoint": endpoint, "transport": transport},
        )
        return server

    async def import_manifests(self, *, actor: User) -> list[DiscoveryResult]:
        """Register every MCP server declared in `mcp/manifests/`, then discover its tools.

        Declarative registration, the same pattern as model manifests: the manifests ship
        with the offline bundle alongside the images `make mcp-vendor` built, so an operator
        does not retype endpoints on a machine with no copy-paste from the outside world.

        Converges rather than duplicating — an existing server is updated in place, so
        re-running after a bundle upgrade is safe. And discovery still grants nothing: every
        tool arrives disabled at HIGH risk regardless of how the server was registered.
        """
        manifests = await asyncio.to_thread(
            _read_mcp_manifests, Path(self._settings.mcp.manifest_path)
        )
        if manifests is None:
            log.info("no_mcp_manifest_directory", path=self._settings.mcp.manifest_path)
            return []

        results: list[DiscoveryResult] = []
        for filename, spec in manifests:
            name = str(spec.get("name") or "").strip()
            if not name:
                log.warning("mcp_manifest_without_name", file=filename)
                continue

            # The endpoint is derived, not declared. `make mcp-vendor` names the compose
            # service `mcp-<name>`, and letting a manifest set an arbitrary endpoint would
            # reintroduce exactly the hand-maintained, drift-prone field this replaces.
            endpoint = f"http://mcp-{name}:8000/"

            server = await self._servers.get_by_name(name)
            if server is None:
                server = McpServer(
                    name=name,
                    description=str(spec.get("description") or spec.get("display_name") or ""),
                    transport="HTTP",
                    endpoint=endpoint,
                )
                self._servers.add(server)
                await self._servers.flush()
                await self._audit.record(
                    AuditAction.MCP_SERVER_REGISTERED,
                    user_id=actor.id,
                    username=actor.username,
                    resource_type="mcp_server",
                    resource_id=str(server.id),
                    metadata={"name": name, "endpoint": endpoint, "source": filename},
                )
            else:
                server.endpoint = endpoint
                server.description = str(spec.get("description") or server.description or "")

            results.append(await self.discover(server.id, actor=actor))

        return results

    async def check_health(self, server_id: uuid.UUID) -> McpServer:
        """Probe by asking for its tool list.

        `tools/list` rather than a health endpoint: MCP does not define one, and a server
        that answers TCP but cannot list its tools is not usable by an agent — which is
        the only sense of "healthy" that matters here.
        """
        server = await self.get_server(server_id)
        server.last_checked_at = dt.datetime.now(dt.UTC)
        try:
            result = await call_mcp(
                server.endpoint, "tools/list", {}, headers=self._auth_headers(server)
            )
        except McpError as exc:
            server.status = "UNREACHABLE"
            server.status_detail = str(exc)[:500]
            return server

        server.status = "HEALTHY"
        server.status_detail = f"{len(result.get('tools', []))} tool(s) offered."
        return server

    async def discover(self, server_id: uuid.UUID, *, actor: User) -> DiscoveryResult:
        """Catalogue the server's tools. **Grants nothing.**

        Discovered tools arrive disabled and HIGH risk. Enabling one, lowering its risk,
        and assigning it to an agent are three separate deliberate acts — otherwise
        registering a server would silently widen what every agent on the platform can do.
        """
        server = await self.get_server(server_id)
        try:
            result = await call_mcp(
                server.endpoint, "tools/list", {}, headers=self._auth_headers(server)
            )
        except McpError as exc:
            server.status = "UNREACHABLE"
            server.status_detail = str(exc)[:500]
            return DiscoveryResult(server.name, 0, 0, 0, detail=f"Discovery failed: {exc}")

        offered = result.get("tools") or []
        existing = {t.name: t for t in await self._tools.list_for_server(server.id)}
        created = updated = 0
        unusable: list[str] = []
        taken: list[str] = []

        for entry in offered:
            remote_name = str(entry.get("name") or "").strip()
            if not remote_name:
                continue
            # Namespaced: two servers may both offer `search`, and tool names are unique.
            local_name = f"{server.name}.{remote_name}"

            # That uniqueness is fleet-wide, but `existing` holds only this server's tools,
            # so a name owned by anything else is invisible to it. Registering it anyway
            # fails the flush and turns the whole discovery into a 500. Skipped rather than
            # adopted: claiming an operator's hand-made tool would silently repoint it at
            # this server's endpoint, which is worse than refusing the one entry.
            if local_name not in existing and await self._tools.get_by_name(local_name):
                taken.append(local_name)
                continue

            schema = entry.get("inputSchema") or {}
            if not _schema_is_usable(schema):
                # Some MCP servers emit a schema with no properties — the platform hands
                # that to the model verbatim, so the model calls the tool with no arguments
                # and the server rejects every call. Recorded on the tool, because
                # "registered but unusable" is otherwise indistinguishable from "registered"
                # until an agent fails against it mid-conversation.
                unusable.append(local_name)

            tool = existing.get(local_name)
            if tool is None:
                tool = Tool(
                    name=local_name,
                    display_name=remote_name,
                    description=str(entry.get("description") or remote_name),
                    type=ToolType.MCP,
                    parameters_schema=schema,
                    required_permission="tool.execute",
                    risk_level=DISCOVERED_RISK_LEVEL,
                    endpoint=server.endpoint,
                    config={"mcp_tool_name": remote_name},
                    mcp_server_id=server.id,
                    # Disabled on arrival. Nobody has read what it does yet.
                    enabled=False,
                    discovered_at=dt.datetime.now(dt.UTC),
                )
                self._tools.add(tool)
                created += 1
                # Recorded immediately, not just counted: a server that lists one name
                # twice would otherwise miss it here and build a second row with the same
                # name. The row is not flushed yet, so the lookup above cannot see it.
                existing[local_name] = tool
            else:
                # Refreshed, but risk level, permission and enabled state are left alone:
                # those are operator decisions, and a re-discovery must not undo them.
                tool.description = str(entry.get("description") or tool.description)
                tool.parameters_schema = schema or tool.parameters_schema
                tool.endpoint = server.endpoint
                tool.discovered_at = dt.datetime.now(dt.UTC)
                updated += 1

        await self._tools.flush()
        server.last_discovered_at = dt.datetime.now(dt.UTC)
        server.status = "HEALTHY"

        await self._audit.record(
            AuditAction.MCP_TOOLS_DISCOVERED,
            user_id=actor.id,
            username=actor.username,
            resource_type="mcp_server",
            resource_id=str(server.id),
            metadata={"found": len(offered), "created": created, "updated": updated},
        )
        details: list[str] = []
        if created:
            details.append(
                f"{created} new tool(s) catalogued, disabled and marked "
                f"{DISCOVERED_RISK_LEVEL} until reviewed."
            )
        if taken:
            details.append(
                f"{len(taken)} tool(s) skipped — the name is already taken on this "
                f"platform: {', '.join(sorted(taken)[:5])}"
                + ("…" if len(taken) > 5 else "")
                + ". Rename or remove the existing tool, then discover again."
            )
            log.warning(
                "mcp_tool_name_already_taken",
                server=server.name,
                tools=sorted(taken)[:10],
            )
        if unusable:
            details.append(
                f"{len(unusable)} tool(s) declare no parameters and will be called with no "
                f"arguments: {', '.join(sorted(unusable)[:5])}"
                + ("…" if len(unusable) > 5 else "")
                + ". This is the server's own schema; check its version."
            )
            log.warning(
                "mcp_tools_without_usable_schema",
                server=server.name,
                tools=sorted(unusable)[:10],
            )

        return DiscoveryResult(
            server.name, len(offered), created, updated, detail=" ".join(details) or None
        )

    async def delete_server(self, server_id: uuid.UUID) -> None:
        """Deletes the server and, by cascade, the tools discovered from it.

        Cascade is correct here: a discovered tool without its server has no endpoint to
        call, and leaving it behind would offer agents a tool that can only fail.
        """
        await self._servers.delete(await self.get_server(server_id))

    def _auth_headers(self, server: McpServer) -> dict[str, str]:
        if not server.credentials_encrypted:
            return {}
        try:
            return {"Authorization": f"Bearer {self._cipher.decrypt(server.credentials_encrypted)}"}
        except Exception:
            log.exception("mcp_server_credential_undecryptable", server=server.name)
            return {}


class SkillRegistryService:
    def __init__(self, skills: SkillRepository) -> None:
        self._skills = skills

    async def list_skills(self) -> list[Skill]:
        return list(await self._skills.list_all())

    async def get_skill(self, skill_id: uuid.UUID) -> Skill:
        skill = await self._skills.get(skill_id)
        if skill is None:
            raise NotFoundError(f"No skill with id {skill_id}.")
        return skill

    async def create_skill(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
        instructions: str,
        version: str = "1.0",
        required_tools: list[str] | None = None,
        required_knowledge: list[str] | None = None,
        parameters: dict | None = None,
        required_permission: str | None = None,
    ) -> Skill:
        if await self._skills.get_by_name(name):
            raise ConflictError(f"A skill named {name!r} already exists.")
        skill = Skill(
            name=name,
            display_name=display_name,
            description=description,
            instructions=instructions,
            version=version,
            required_tools=required_tools or [],
            required_knowledge=required_knowledge or [],
            parameters=parameters or {},
            required_permission=required_permission,
        )
        self._skills.add(skill)
        await self._skills.flush()
        return skill

    async def delete_skill(self, skill_id: uuid.UUID) -> None:
        await self._skills.delete(await self.get_skill(skill_id))


class AgentRegistryService:
    def __init__(
        self,
        settings: Settings,
        agents: AgentRepository,
        versions: AgentVersionRepository,
        tools: ToolRepository,
        skills: SkillRepository,
        audit: AuditService,
    ) -> None:
        self._settings = settings
        self._agents = agents
        self._versions = versions
        self._tools = tools
        self._skills = skills
        self._audit = audit

    async def list_agents(self, *, enabled_only: bool = False) -> list[Agent]:
        return list(await self._agents.list_all(enabled_only=enabled_only))

    async def get_agent(self, agent_id: uuid.UUID) -> Agent:
        agent = await self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError(f"No agent with id {agent_id}.")
        return agent

    async def get_by_slug(self, slug: str) -> Agent:
        agent = await self._agents.get_by_slug(slug)
        if agent is None:
            raise NotFoundError(f"No agent with slug {slug!r}.")
        return agent

    async def create_agent(
        self,
        *,
        slug: str,
        display_name: str,
        description: str | None,
        system_prompt: str,
        model: str,
        temperature: float,
        max_iterations: int,
        tool_ids: list[uuid.UUID],
        skill_ids: list[uuid.UUID],
        knowledge_base_ids: list[str],
        memory_enabled: bool,
        actor: User,
    ) -> tuple[Agent, AgentVersion]:
        if not SLUG_PATTERN.match(slug):
            raise ValidationError(
                "A slug must be lowercase letters, digits and hyphens, 3-64 characters.",
                details={"field": "slug"},
            )
        if await self._agents.get_by_slug(slug):
            raise ConflictError(f"An agent with slug {slug!r} already exists.")

        agent = Agent(
            slug=slug,
            display_name=display_name,
            description=description,
            owner_id=actor.id,
            current_version=1,
        )
        self._agents.add(agent)
        await self._agents.flush()

        version = await self._create_version(
            agent,
            version=1,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_iterations=max_iterations,
            tool_ids=tool_ids,
            skill_ids=skill_ids,
            knowledge_base_ids=knowledge_base_ids,
            memory_enabled=memory_enabled,
            actor=actor,
            change_note="Initial version.",
        )

        await self._audit.record(
            AuditAction.AGENT_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="agent",
            resource_id=str(agent.id),
            metadata={"slug": slug, "model": model, "tools": len(tool_ids)},
        )
        return agent, version

    async def create_version(
        self,
        agent_id: uuid.UUID,
        *,
        actor: User,
        change_note: str | None,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_iterations: int | None = None,
        tool_ids: list[uuid.UUID] | None = None,
        skill_ids: list[uuid.UUID] | None = None,
        knowledge_base_ids: list[str] | None = None,
        memory_enabled: bool | None = None,
    ) -> AgentVersion:
        """Publish a new version. The previous one is left untouched.

        The agent's ``current_version`` moves; nothing else does. Runs already in flight
        keep executing the version they started on, because they hold its id.
        """
        agent = await self.get_agent(agent_id)
        current = await self._versions.get_current(agent_id)
        if current is None:
            raise NotFoundError(f"Agent {agent.slug} has no current version.")

        number = await self._versions.next_version(agent_id)
        version = await self._create_version(
            agent,
            version=number,
            # Omitted fields inherit from the current version. A partial update must not
            # silently blank an agent's tool grants — the natural bug when every field is
            # treated as "None means empty".
            system_prompt=system_prompt if system_prompt is not None else current.system_prompt,
            model=model or current.model,
            temperature=temperature if temperature is not None else current.temperature,
            max_iterations=max_iterations or current.max_iterations,
            tool_ids=(tool_ids if tool_ids is not None else [t.tool_id for t in current.tools]),
            skill_ids=(
                skill_ids if skill_ids is not None else [s.skill_id for s in current.skills]
            ),
            knowledge_base_ids=(
                knowledge_base_ids
                if knowledge_base_ids is not None
                else list(current.knowledge_base_ids)
            ),
            memory_enabled=(
                memory_enabled if memory_enabled is not None else current.memory_enabled
            ),
            actor=actor,
            change_note=change_note,
        )
        agent.current_version = number

        await self._audit.record(
            AuditAction.AGENT_VERSION_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="agent",
            resource_id=str(agent.id),
            metadata={"slug": agent.slug, "version": number, "note": change_note},
        )
        return version

    async def _create_version(
        self,
        agent: Agent,
        *,
        version: int,
        system_prompt: str,
        model: str,
        temperature: float,
        max_iterations: int,
        tool_ids: list[uuid.UUID],
        skill_ids: list[uuid.UUID],
        knowledge_base_ids: list[str],
        memory_enabled: bool,
        actor: User,
        change_note: str | None,
    ) -> AgentVersion:
        # Every referenced tool and skill must exist *now*. A version holding a dangling
        # reference would fail at run time, when a user is waiting for an answer.
        for tool_id in tool_ids:
            if await self._tools.get(tool_id) is None:
                raise ValidationError(f"No tool with id {tool_id}.")
        for skill_id in skill_ids:
            if await self._skills.get(skill_id) is None:
                raise ValidationError(f"No skill with id {skill_id}.")

        row = AgentVersion(
            agent_id=agent.id,
            version=version,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_iterations=min(max_iterations, self._settings.agents.max_iterations),
            knowledge_base_ids=knowledge_base_ids,
            memory_enabled=memory_enabled,
            created_by=actor.id,
            change_note=change_note,
        )
        self._versions.add(row)
        await self._versions.flush()

        for tool_id in tool_ids:
            self._versions.session.add(AgentTool(agent_version_id=row.id, tool_id=tool_id))
        for skill_id in skill_ids:
            self._versions.session.add(AgentSkill(agent_version_id=row.id, skill_id=skill_id))
        await self._versions.flush()
        return row

    async def current_version(self, agent_id: uuid.UUID) -> AgentVersion | None:
        return await self._versions.get_current(agent_id)

    async def version_read(self, version: AgentVersion) -> AgentVersionRead:
        """Project a version, resolving tool and skill names.

        Async and on the service because the names come from relationships: doing it in
        the router would either lazy-load outside the session or need the router to know
        about the ORM, and the layering contract forbids the second.
        """
        resolved = await self._versions.get_resolved(version.id) or version
        # Built explicitly rather than by `model_validate`. The schema's `tools` and
        # `skills` are lists of *names*, but the ORM attributes of those names are
        # association objects — from_attributes would try to validate an `AgentTool` as a
        # string and fail before the names could be substituted.
        return AgentVersionRead(
            id=resolved.id,
            version=resolved.version,
            system_prompt=resolved.system_prompt,
            model=resolved.model,
            temperature=resolved.temperature,
            max_iterations=resolved.max_iterations,
            knowledge_base_ids=list(resolved.knowledge_base_ids),
            memory_enabled=resolved.memory_enabled,
            change_note=resolved.change_note,
            created_at=resolved.created_at,
            tools=[t.tool.name for t in resolved.tools],
            skills=[s.skill.name for s in resolved.skills],
        )

    async def update_agent(
        self, agent_id: uuid.UUID, payload: object, *, actor: User
    ) -> tuple[Agent, AgentVersion]:
        """Apply an update by publishing a new version.

        Identity fields (display name, description, enabled) live on the agent and are
        updated in place — they do not change what the agent *does*, so versioning them
        would produce noise in the history. Everything executable produces a version.
        """
        agent = await self.get_agent(agent_id)

        for field in ("display_name", "description", "enabled"):
            value = getattr(payload, field, None)
            if value is not None:
                setattr(agent, field, value)

        version = await self.create_version(
            agent_id,
            actor=actor,
            change_note=getattr(payload, "change_note", None),
            system_prompt=getattr(payload, "system_prompt", None),
            model=getattr(payload, "model", None),
            temperature=getattr(payload, "temperature", None),
            max_iterations=getattr(payload, "max_iterations", None),
            tool_ids=getattr(payload, "tool_ids", None),
            skill_ids=getattr(payload, "skill_ids", None),
            knowledge_base_ids=getattr(payload, "knowledge_base_ids", None),
            memory_enabled=getattr(payload, "memory_enabled", None),
        )

        await self._audit.record(
            AuditAction.AGENT_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="agent",
            resource_id=str(agent.id),
            metadata={"slug": agent.slug, "new_version": version.version},
        )
        return agent, version

    async def list_versions(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        return list(await self._versions.list_for_agent(agent_id))

    async def delete_agent(self, agent_id: uuid.UUID, *, actor: User) -> None:
        agent = await self.get_agent(agent_id)
        await self._agents.delete(agent)
        await self._audit.record(
            AuditAction.AGENT_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="agent",
            resource_id=str(agent_id),
            metadata={"slug": agent.slug},
        )

    # -- assembly ----------------------------------------------------------
    async def build_spec(self, agent: Agent, version: AgentVersion) -> AgentSpec:
        """Resolve a version into the fully-formed spec the runtime executes.

        The runtime performs no lookups of its own (§28), so everything it needs is
        assembled here: the prompt with its skills folded in, and the tool definitions.
        """
        prompt = _compose_prompt(version)
        definitions = []
        for granted in version.tools:
            tool = granted.tool
            if not tool.enabled:
                # Skipped rather than offered-and-refused: telling the model about a tool
                # it will always be denied wastes an iteration every single run.
                continue
            definition = _definition(tool)
            # The pipeline authorises by id, and the model only ever sees names. Carrying
            # the id through config is what lets the runtime map one to the other without
            # a second lookup against a possibly-different version.
            definition.config["_tool_id"] = str(tool.id)
            if tool.credentials_encrypted:
                definition.config["_credentials_encrypted"] = tool.credentials_encrypted
            definitions.append(definition)

        return AgentSpec(
            agent_id=str(agent.id),
            version=version.version,
            name=agent.display_name,
            model=version.model,
            system_prompt=prompt,
            tools=tuple(definitions),
            knowledge_base_ids=tuple(version.knowledge_base_ids),
            memory_enabled=version.memory_enabled,
            max_iterations=version.max_iterations,
            temperature=version.temperature,
        )


def _schema_is_usable(schema: dict) -> bool:
    """Would a model be able to call a tool with this schema?

    A JSON Schema with no properties tells the model the tool takes no arguments, so it
    calls it with none and the server rejects it — every time. Some MCP servers emit exactly
    that, and registering the tool without noticing produces an agent that fails only when
    it finally tries to use it.
    """
    if not isinstance(schema, dict):
        return False
    if schema.get("properties"):
        return True
    # A `$ref`/`$defs` schema is indirect but genuinely usable.
    return bool(
        schema.get("$ref") or schema.get("$defs") or schema.get("oneOf") or schema.get("anyOf")
    )


def _read_mcp_manifests(directory: Path) -> list[tuple[str, dict]] | None:
    """Read MCP manifests as (filename, spec). ``None`` if the directory is absent.

    Blocking file I/O, so callers run it in a thread — the same treatment as model
    manifests.
    """
    if not directory.is_dir():
        return None

    out: list[tuple[str, dict]] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.warning("mcp_manifest_unreadable", file=path.name, error=str(exc)[:200])
            continue
        if isinstance(spec, dict):
            out.append((path.name, spec))
    return out


def _compose_prompt(version: AgentVersion) -> str:
    """Fold the agent's skills into its system prompt (§M11).

    Skills are appended rather than replacing the prompt: the agent's own instructions
    set its character and boundaries, and a skill adds a capability within them. The
    other order would let a shared skill silently override an agent's constraints.
    """
    parts = [version.system_prompt.strip()]
    for granted in version.skills:
        skill = granted.skill
        if not skill.enabled:
            continue
        parts.append(
            f"\n## Skill: {skill.display_name}\n{skill.description.strip()}\n\n"
            f"{skill.instructions.strip()}"
        )
    return "\n".join(parts)
