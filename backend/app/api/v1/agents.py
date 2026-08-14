"""Agent, skill, tool, MCP and run endpoints (M10-M14, §8)."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import (
    AgentRegistryDep,
    AgentRunServiceDep,
    DefinitionImporterDep,
    McpRegistryDep,
    SkillRegistryDep,
    ToolRegistryDep,
    require_permission,
)
from app.config.settings import get_settings
from app.core.permissions import Permission as Perm
from app.models.agents import Tool
from app.models.auth import User
from app.schemas.agents import (
    AgentCreateRequest,
    AgentDetail,
    AgentExecuteRequest,
    AgentRead,
    AgentUpdateRequest,
    AgentVersionRead,
    ApprovalRequest,
    DefinitionImportResponse,
    DiscoveryResponse,
    McpServerCreateRequest,
    McpServerRead,
    PendingApprovalRead,
    RunDetail,
    RunEventRead,
    RunRead,
    SkillCreateRequest,
    SkillRead,
    ToolCreateRequest,
    ToolExecutionRead,
    ToolRead,
    ToolUpdateRequest,
)
from app.schemas.common import MessageResponse

router = APIRouter(tags=["agents"])


# ---------------------------------------------------------------------------
# Tools (M12)
# ---------------------------------------------------------------------------
def _tool_read(tool: Tool) -> ToolRead:
    detail = ToolRead.model_validate(tool)
    # Surfaced on the tool rather than left for the caller to infer from risk_level plus
    # a config setting they cannot see.
    detail.requires_approval = (
        tool.risk_level in get_settings().agents.approval_required_risk_levels
    )
    return detail


@router.get("/tools", response_model=list[ToolRead], summary="List registered tools")
async def list_tools(
    service: ToolRegistryDep,
    _actor: Annotated[User, require_permission(Perm.TOOL_VIEW)],
) -> list[ToolRead]:
    return [_tool_read(t) for t in await service.list_tools()]


@router.post(
    "/tools",
    response_model=ToolRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a tool",
)
async def register_tool(
    payload: ToolCreateRequest,
    service: ToolRegistryDep,
    actor: Annotated[User, require_permission(Perm.TOOL_MANAGE)],
) -> ToolRead:
    """Catalogue a tool an agent may then be granted.

    Registering grants nothing. A tool becomes reachable only when an agent version lists
    it *and* the invoking user holds its `required_permission` (§10).
    """
    return _tool_read(
        await service.register_tool(
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            tool_type=payload.type,
            parameters_schema=payload.parameters_schema,
            required_permission=payload.required_permission,
            risk_level=payload.risk_level,
            endpoint=payload.endpoint,
            config=payload.config,
            credentials=payload.credentials,
            timeout_seconds=payload.timeout_seconds,
            actor=actor,
        )
    )


@router.put("/tools/{tool_id}", response_model=ToolRead, summary="Update a tool")
async def update_tool(
    payload: ToolUpdateRequest,
    service: ToolRegistryDep,
    actor: Annotated[User, require_permission(Perm.TOOL_MANAGE)],
    tool_id: uuid.UUID = Path(...),
) -> ToolRead:
    """Change what an operator may change. Not the type or parameter schema — those
    define what the tool is, and altering them under an agent already granted it would
    silently change what that agent can do."""
    return _tool_read(await service.update_tool(tool_id, payload, actor=actor))


@router.post("/tools/{tool_id}/test", response_model=MessageResponse, summary="Test a tool")
async def test_tool(
    service: ToolRegistryDep,
    _actor: Annotated[User, require_permission(Perm.TOOL_MANAGE)],
    tool_id: uuid.UUID = Path(...),
) -> MessageResponse:
    """Validate the definition against its executor, so a broken endpoint or credential
    surfaces now rather than three tool calls into someone's conversation."""
    return MessageResponse(message=await service.test_tool(tool_id))


@router.delete("/tools/{tool_id}", response_model=MessageResponse, summary="Delete a tool")
async def delete_tool(
    service: ToolRegistryDep,
    actor: Annotated[User, require_permission(Perm.TOOL_MANAGE)],
    tool_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_tool(tool_id, actor=actor)
    return MessageResponse(message="Tool deleted. Agents granted it lose access immediately.")


# ---------------------------------------------------------------------------
# MCP servers (M13)
# ---------------------------------------------------------------------------
@router.get("/mcp/servers", response_model=list[McpServerRead], summary="List MCP servers")
async def list_mcp_servers(
    service: McpRegistryDep,
    tools: ToolRegistryDep,
    _actor: Annotated[User, require_permission(Perm.MCP_VIEW)],
) -> list[McpServerRead]:
    all_tools = await tools.list_tools()
    result = []
    for server in await service.list_servers():
        detail = McpServerRead.model_validate(server)
        detail.tool_count = sum(1 for t in all_tools if t.mcp_server_id == server.id)
        result.append(detail)
    return result


@router.post(
    "/definitions/import",
    response_model=list[DefinitionImportResponse],
    summary="Import the shipped agent, skill and tool definitions",
)
async def import_definitions(
    importer: DefinitionImporterDep,
    actor: Annotated[User, require_permission(Perm.AGENT_CREATE)],
) -> list[DefinitionImportResponse]:
    """Declarative registration from `tools/`, `skills/` and `agents/` (M10-M12).

    How an air-gapped install catalogues its agents: the manifests ship inside the
    bundle, so nobody retypes a system prompt on a machine with no outside copy-paste,
    and two sites given the same bundle get the same agents.

    Converges rather than duplicating. Re-importing an edited manifest publishes a new
    agent *version* — never an in-place edit, because a run records the version it
    executed and rewriting it would make an old run unexplainable (§M14).

    Requires `agent.create`, the strongest of the permissions this touches: one call
    registers tools, skills and agents together, so it must not be reachable by someone
    entitled only to the least of those.
    """
    return [
        DefinitionImportResponse(**asdict(result))
        for result in await importer.import_all(actor=actor)
    ]


@router.post(
    "/mcp/servers",
    response_model=McpServerRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register an MCP server",
)
async def register_mcp_server(
    payload: McpServerCreateRequest,
    service: McpRegistryDep,
    actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
) -> McpServerRead:
    return McpServerRead.model_validate(
        await service.register_server(
            name=payload.name,
            endpoint=payload.endpoint,
            description=payload.description,
            transport=payload.transport,
            credentials=payload.credentials,
            actor=actor,
        )
    )


@router.post(
    "/mcp/servers/import-manifests",
    response_model=list[DiscoveryResponse],
    summary="Register every MCP server described by a manifest",
)
async def import_mcp_manifests(
    service: McpRegistryDep,
    actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
) -> list[DiscoveryResponse]:
    """Declarative registration from `mcp/manifests/`, then discovery.

    How an air-gapped install adopts open-source MCP servers. `make mcp-vendor` bakes each
    server's package into its own image on a connected build machine; the images and these
    manifests ship in the bundle; this registers them and catalogues their tools without
    anyone retyping an endpoint.

    Converges — re-running after a bundle upgrade updates rather than duplicates. And it
    grants nothing: every discovered tool arrives **disabled at HIGH risk** regardless of
    how its server was registered.
    """
    return [
        DiscoveryResponse(**asdict(result))
        for result in await service.import_manifests(actor=actor)
    ]


@router.post(
    "/mcp/servers/{server_id}/health",
    response_model=McpServerRead,
    summary="Health-check an MCP server",
)
async def check_mcp_server(
    service: McpRegistryDep,
    _actor: Annotated[User, require_permission(Perm.MCP_VIEW)],
    server_id: uuid.UUID = Path(...),
) -> McpServerRead:
    """Probes by asking for the tool list. MCP defines no health endpoint, and a server
    that answers TCP but cannot list its tools is not usable by an agent."""
    return McpServerRead.model_validate(await service.check_health(server_id))


@router.post(
    "/mcp/servers/{server_id}/discover",
    response_model=DiscoveryResponse,
    summary="Discover an MCP server's tools",
)
async def discover_mcp_tools(
    service: McpRegistryDep,
    actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
    server_id: uuid.UUID = Path(...),
) -> DiscoveryResponse:
    """Catalogue what the server offers. **Grants nothing** (§M13).

    Discovered tools arrive **disabled** and marked HIGH risk. Enabling one, lowering its
    risk, and assigning it to an agent are three separate deliberate acts — otherwise
    registering a server would silently widen what every agent on the platform can do.
    """
    return DiscoveryResponse(**asdict(await service.discover(server_id, actor=actor)))


@router.delete(
    "/mcp/servers/{server_id}", response_model=MessageResponse, summary="Delete an MCP server"
)
async def delete_mcp_server(
    service: McpRegistryDep,
    _actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
    server_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_server(server_id)
    return MessageResponse(message="MCP server deleted, along with the tools discovered from it.")


# ---------------------------------------------------------------------------
# Skills (M11)
# ---------------------------------------------------------------------------
@router.get("/skills", response_model=list[SkillRead], summary="List skills")
async def list_skills(
    service: SkillRegistryDep,
    _actor: Annotated[User, require_permission(Perm.SKILL_VIEW)],
) -> list[SkillRead]:
    return [SkillRead.model_validate(s) for s in await service.list_skills()]


@router.post(
    "/skills",
    response_model=SkillRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a skill",
)
async def create_skill(
    payload: SkillCreateRequest,
    service: SkillRegistryDep,
    _actor: Annotated[User, require_permission(Perm.SKILL_MANAGE)],
) -> SkillRead:
    return SkillRead.model_validate(
        await service.create_skill(
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            instructions=payload.instructions,
            version=payload.version,
            required_tools=payload.required_tools,
            required_knowledge=payload.required_knowledge,
            parameters=payload.parameters,
            required_permission=payload.required_permission,
        )
    )


@router.delete("/skills/{skill_id}", response_model=MessageResponse, summary="Delete a skill")
async def delete_skill(
    service: SkillRegistryDep,
    _actor: Annotated[User, require_permission(Perm.SKILL_MANAGE)],
    skill_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_skill(skill_id)
    return MessageResponse(message="Skill deleted.")


# ---------------------------------------------------------------------------
# Agents (M10)
# ---------------------------------------------------------------------------
@router.get("/agents", response_model=list[AgentRead], summary="List agents")
async def list_agents(
    service: AgentRegistryDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
) -> list[AgentRead]:
    return [AgentRead.model_validate(a) for a in await service.list_agents()]


@router.post(
    "/agents",
    response_model=AgentDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent",
)
async def create_agent(
    payload: AgentCreateRequest,
    service: AgentRegistryDep,
    actor: Annotated[User, require_permission(Perm.AGENT_CREATE)],
) -> AgentDetail:
    agent, version = await service.create_agent(
        slug=payload.slug,
        display_name=payload.display_name,
        description=payload.description,
        system_prompt=payload.system_prompt,
        model=payload.model,
        temperature=payload.temperature,
        max_iterations=payload.max_iterations,
        tool_ids=payload.tool_ids,
        skill_ids=payload.skill_ids,
        knowledge_base_ids=payload.knowledge_base_ids,
        memory_enabled=payload.memory_enabled,
        actor=actor,
    )
    detail = AgentDetail.model_validate(agent)
    detail.version = await service.version_read(version)
    return detail


@router.get("/agents/{agent_id}", response_model=AgentDetail, summary="Get an agent")
async def get_agent(
    service: AgentRegistryDep,
    runs: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
    agent_id: uuid.UUID = Path(...),
) -> AgentDetail:
    agent = await service.get_agent(agent_id)
    version = await service.current_version(agent_id)
    detail = AgentDetail.model_validate(agent)
    if version is not None:
        detail.version = await service.version_read(version)
        detail.unavailable_tools = [t.tool.name for t in version.tools if not t.tool.enabled]
    detail.run_count = len(await runs.list_runs(agent_id, limit=1000))
    return detail


@router.put("/agents/{agent_id}", response_model=AgentDetail, summary="Update an agent")
async def update_agent(
    payload: AgentUpdateRequest,
    service: AgentRegistryDep,
    actor: Annotated[User, require_permission(Perm.AGENT_EDIT)],
    agent_id: uuid.UUID = Path(...),
) -> AgentDetail:
    """Publishes a **new version**. The previous one is never mutated, which is what lets
    a run from three months ago still be explained — and runs already in flight continue
    on the version they started."""
    agent, version = await service.update_agent(agent_id, payload, actor=actor)
    detail = AgentDetail.model_validate(agent)
    detail.version = await service.version_read(version)
    return detail


@router.get(
    "/agents/{agent_id}/versions",
    response_model=list[AgentVersionRead],
    summary="Agent version history",
)
async def list_agent_versions(
    service: AgentRegistryDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
    agent_id: uuid.UUID = Path(...),
) -> list[AgentVersionRead]:
    return [await service.version_read(v) for v in await service.list_versions(agent_id)]


@router.delete("/agents/{agent_id}", response_model=MessageResponse, summary="Delete an agent")
async def delete_agent(
    service: AgentRegistryDep,
    actor: Annotated[User, require_permission(Perm.AGENT_DELETE)],
    agent_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await service.delete_agent(agent_id, actor=actor)
    return MessageResponse(message="Agent deleted. Its runs are retained for audit.")


# ---------------------------------------------------------------------------
# Runs (M14, §11)
# ---------------------------------------------------------------------------
@router.post("/agents/{agent_id}/execute", summary="Run an agent")
async def execute_agent(
    payload: AgentExecuteRequest,
    registry: AgentRegistryDep,
    service: AgentRunServiceDep,
    actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
    agent_id: uuid.UUID = Path(...),
) -> Any:
    """Execute, buffered or streamed.

    With `stream: true` the response is SSE of the §11 event model, so a UI shows tool
    calls as they happen rather than after the fact. Either way the run is recorded, and
    a run that suspends for approval returns with `state: WAITING_FOR_APPROVAL` rather
    than hanging.
    """
    agent = await registry.get_agent(agent_id)
    run, events = await service.start(
        agent, message=payload.message, actor=actor, conversation_id=payload.conversation_id
    )

    if payload.stream:

        async def sse() -> Any:
            async for event in events:
                frame = {
                    "type": str(event.type),
                    "sequence": event.sequence,
                    "payload": event.payload,
                    "duration_ms": event.duration_ms,
                }
                yield f"data: {json.dumps(frame)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Drained here rather than discarded: the events are what drive the run to completion,
    # and the persistence happens as they pass.
    async for _ in events:
        pass
    return await _run_detail(service, run.id)


@router.get("/agents/{agent_id}/runs", response_model=list[RunRead], summary="Runs of an agent")
async def list_agent_runs(
    service: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
    agent_id: uuid.UUID = Path(...),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunRead]:
    return [RunRead.model_validate(r) for r in await service.list_runs(agent_id, limit=limit)]


# Declared before /runs/{run_id}: FastAPI matches in order, and this literal path
# would otherwise be parsed as a run id and rejected as a malformed UUID.
@router.get(
    "/runs/pending-approvals",
    response_model=list[PendingApprovalRead],
    summary="Tool calls awaiting approval",
)
async def list_pending_approvals(
    service: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.TOOL_APPROVE)],
) -> list[PendingApprovalRead]:
    """The approver's queue. Requires `tool.approve`, not `agent.view`: seeing what is
    waiting is part of the approval privilege."""
    return [PendingApprovalRead.model_validate(e) for e in await service.list_pending_approvals()]


@router.get("/runs/{run_id}", response_model=RunDetail, summary="Get a run with its trace")
async def get_run(
    service: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
    run_id: uuid.UUID = Path(...),
) -> RunDetail:
    return await _run_detail(service, run_id)


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead], summary="Run events")
async def list_run_events(
    service: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_VIEW)],
    run_id: uuid.UUID = Path(...),
) -> list[RunEventRead]:
    return [RunEventRead.model_validate(e) for e in await service.list_events(run_id)]


@router.post("/runs/{run_id}/cancel", response_model=RunRead, summary="Cancel a run")
async def cancel_run(
    service: AgentRunServiceDep,
    _actor: Annotated[User, require_permission(Perm.AGENT_EXECUTE)],
    run_id: uuid.UUID = Path(...),
) -> RunRead:
    return RunRead.model_validate(await service.cancel(run_id))


@router.post("/runs/{run_id}/approve", response_model=RunDetail, summary="Approve or refuse")
async def approve_run(
    payload: ApprovalRequest,
    service: AgentRunServiceDep,
    actor: Annotated[User, require_permission(Perm.TOOL_APPROVE)],
    run_id: uuid.UUID = Path(...),
) -> RunDetail:
    """Answer a pending approval and let the run continue.

    Requires **`tool.approve`**, deliberately separate from `tool.execute` (§10, §M24):
    approving a HIGH-risk action is a different privilege from performing a routine one,
    and collapsing them would let any tool user approve their own privileged call.

    A refusal does not kill the run — the agent is told and carries on, which may well
    produce a useful answer by another route.
    """
    _, events = await service.approve(
        run_id, approved=payload.approved, actor=actor, reason=payload.reason
    )
    async for _ in events:
        pass
    return await _run_detail(service, run_id)


async def _run_detail(service: Any, run_id: uuid.UUID) -> RunDetail:
    run = await service.get_run(run_id)
    summary = await service.summary(run)
    detail = RunDetail.model_validate(run)
    detail.agent_slug = summary.agent_slug
    detail.pending_tool = summary.pending_tool
    detail.pending_arguments = summary.pending_arguments
    detail.events = [RunEventRead.model_validate(e) for e in await service.list_events(run_id)]
    detail.tool_calls = [
        ToolExecutionRead.model_validate(e) for e in await service.list_executions(run_id)
    ]
    return detail
