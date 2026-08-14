"""The platform's permission and role catalogue (§M03).

Single source of truth. Routes reference :class:`Permission` constants rather than
string literals, so a renamed permission becomes an import error rather than a
route that silently authorises nobody — or, far worse, everybody.

The catalogue covers all 28 modules from the outset even though most of those
routes do not exist yet. Defining the vocabulary once means each later module
declares the permission it needs instead of inventing a naming scheme, which is
how permission sets normally drift into ``model.deploy`` alongside
``deployModel`` and ``models:deploy``.
"""

from __future__ import annotations

from typing import Final


class Permission:
    """Permission name constants, ``resource.action``."""

    # --- infrastructure (M04, M05, M06) ---
    INFRASTRUCTURE_VIEW: Final = "infrastructure.view"
    INFRASTRUCTURE_MANAGE: Final = "infrastructure.manage"
    CONTAINER_VIEW: Final = "container.view"
    CONTAINER_MANAGE: Final = "container.manage"
    GPU_VIEW: Final = "gpu.view"

    # --- models (M07, M08, M09) ---
    MODEL_VIEW: Final = "model.view"
    MODEL_REGISTER: Final = "model.register"
    MODEL_DEPLOY: Final = "model.deploy"
    MODEL_STOP: Final = "model.stop"
    MODEL_DELETE: Final = "model.delete"
    MODEL_INFERENCE: Final = "model.inference"

    # --- agents (M10, M14) ---
    AGENT_VIEW: Final = "agent.view"
    AGENT_CREATE: Final = "agent.create"
    AGENT_EDIT: Final = "agent.edit"
    AGENT_EXECUTE: Final = "agent.execute"
    AGENT_DELETE: Final = "agent.delete"

    # --- skills (M11) ---
    SKILL_VIEW: Final = "skill.view"
    SKILL_MANAGE: Final = "skill.manage"

    # --- tools and MCP (M12, M13) ---
    TOOL_VIEW: Final = "tool.view"
    TOOL_EXECUTE: Final = "tool.execute"
    TOOL_MANAGE: Final = "tool.manage"
    # Separate from TOOL_EXECUTE: approving a HIGH-risk action is a different
    # privilege from performing a routine one (§10, §M24). Collapsing them would
    # let any tool user approve their own privileged call.
    TOOL_APPROVE: Final = "tool.approve"
    MCP_VIEW: Final = "mcp.view"
    MCP_MANAGE: Final = "mcp.manage"

    # --- knowledge (M15, M16) ---
    KNOWLEDGE_VIEW: Final = "knowledge.view"
    KNOWLEDGE_MANAGE: Final = "knowledge.manage"
    DOCUMENT_UPLOAD: Final = "document.upload"
    DOCUMENT_DELETE: Final = "document.delete"

    # --- developer (M20) ---
    APIKEY_VIEW: Final = "apikey.view"
    APIKEY_MANAGE: Final = "apikey.manage"
    USAGE_VIEW: Final = "usage.view"

    # --- administration (M03, M24, M25) ---
    USER_VIEW: Final = "user.view"
    USER_MANAGE: Final = "user.manage"
    ROLE_MANAGE: Final = "role.manage"
    SETTINGS_VIEW: Final = "settings.view"
    SETTINGS_MANAGE: Final = "settings.manage"
    AUDIT_VIEW: Final = "audit.view"
    BACKUP_MANAGE: Final = "backup.manage"

    # --- observability (M19) ---
    MONITORING_VIEW: Final = "monitoring.view"
    TRACE_VIEW: Final = "trace.view"


#: ``permission name -> human description``, used by the seeder and the admin UI.
PERMISSION_CATALOGUE: Final[dict[str, str]] = {
    Permission.INFRASTRUCTURE_VIEW: "View nodes, hosts and their resources",
    Permission.INFRASTRUCTURE_MANAGE: "Register, modify and remove nodes",
    Permission.CONTAINER_VIEW: "View containers and their logs",
    Permission.CONTAINER_MANAGE: "Create, start, stop and remove containers",
    Permission.GPU_VIEW: "View GPU inventory and metrics",
    Permission.MODEL_VIEW: "View the model registry and deployments",
    Permission.MODEL_REGISTER: "Register and import models",
    Permission.MODEL_DEPLOY: "Deploy models onto GPU nodes",
    Permission.MODEL_STOP: "Stop and restart model deployments",
    Permission.MODEL_DELETE: "Delete models and deployments",
    Permission.MODEL_INFERENCE: "Call inference endpoints through the gateway",
    Permission.AGENT_VIEW: "View agents and their run history",
    Permission.AGENT_CREATE: "Create agents",
    Permission.AGENT_EDIT: "Modify agents and publish new versions",
    Permission.AGENT_EXECUTE: "Execute agents",
    Permission.AGENT_DELETE: "Delete agents",
    Permission.SKILL_VIEW: "View skills",
    Permission.SKILL_MANAGE: "Create, version and delete skills",
    Permission.TOOL_VIEW: "View registered tools",
    Permission.TOOL_EXECUTE: "Execute tools",
    Permission.TOOL_MANAGE: "Register, configure, enable and disable tools",
    Permission.TOOL_APPROVE: "Approve high-risk tool executions",
    Permission.MCP_VIEW: "View MCP servers and their discovered tools",
    Permission.MCP_MANAGE: "Register and configure MCP servers",
    Permission.KNOWLEDGE_VIEW: "View knowledge bases",
    Permission.KNOWLEDGE_MANAGE: "Create and delete knowledge bases",
    Permission.DOCUMENT_UPLOAD: "Upload documents for ingestion",
    Permission.DOCUMENT_DELETE: "Delete documents",
    Permission.APIKEY_VIEW: "View own API keys",
    Permission.APIKEY_MANAGE: "Create and revoke API keys",
    Permission.USAGE_VIEW: "View API usage records",
    Permission.USER_VIEW: "View users",
    Permission.USER_MANAGE: "Create, modify and delete users",
    Permission.ROLE_MANAGE: "Manage roles and their permissions",
    Permission.SETTINGS_VIEW: "View platform settings",
    Permission.SETTINGS_MANAGE: "Modify platform settings",
    Permission.AUDIT_VIEW: "View audit logs",
    Permission.BACKUP_MANAGE: "Create, verify and restore backups",
    Permission.MONITORING_VIEW: "View metrics and dashboards",
    Permission.TRACE_VIEW: "View LLM and agent execution traces",
}


class Role:
    """The eight system role names from §M03."""

    SUPER_ADMIN: Final = "SUPER_ADMIN"
    ADMIN: Final = "ADMIN"
    AI_ADMIN: Final = "AI_ADMIN"
    INFRA_ADMIN: Final = "INFRA_ADMIN"
    AGENT_ADMIN: Final = "AGENT_ADMIN"
    DEVELOPER: Final = "DEVELOPER"
    USER: Final = "USER"
    AUDITOR: Final = "AUDITOR"


_ALL_PERMISSIONS: Final = frozenset(PERMISSION_CATALOGUE)

# Read-only permissions, the baseline every non-auditor role builds on.
_VIEWER: Final = frozenset(
    {
        Permission.MODEL_VIEW,
        Permission.AGENT_VIEW,
        Permission.SKILL_VIEW,
        Permission.TOOL_VIEW,
        Permission.KNOWLEDGE_VIEW,
    }
)

#: ``role name -> (description, permissions)``.
#:
#: Grants are least-privilege by design. INFRA_ADMIN cannot create agents;
#: AGENT_ADMIN cannot deploy models or touch infrastructure; AUDITOR can read the
#: audit log but change nothing. The point of eight roles is separation of duty —
#: making them all near-copies of ADMIN would defeat it.
ROLE_DEFINITIONS: Final[dict[str, tuple[str, frozenset[str]]]] = {
    Role.SUPER_ADMIN: (
        "Unrestricted access to the entire platform",
        _ALL_PERMISSIONS,
    ),
    Role.ADMIN: (
        "Platform administration, excluding backup/restore",
        _ALL_PERMISSIONS - {Permission.BACKUP_MANAGE},
    ),
    Role.AI_ADMIN: (
        "Manages models, deployments and knowledge bases",
        frozenset(
            {
                Permission.MODEL_VIEW,
                Permission.MODEL_REGISTER,
                Permission.MODEL_DEPLOY,
                Permission.MODEL_STOP,
                Permission.MODEL_DELETE,
                Permission.MODEL_INFERENCE,
                Permission.KNOWLEDGE_VIEW,
                Permission.KNOWLEDGE_MANAGE,
                Permission.DOCUMENT_UPLOAD,
                Permission.DOCUMENT_DELETE,
                Permission.GPU_VIEW,
                Permission.INFRASTRUCTURE_VIEW,
                Permission.MONITORING_VIEW,
                Permission.TRACE_VIEW,
            }
        )
        | _VIEWER,
    ),
    Role.INFRA_ADMIN: (
        "Manages nodes, GPUs and containers",
        frozenset(
            {
                Permission.INFRASTRUCTURE_VIEW,
                Permission.INFRASTRUCTURE_MANAGE,
                Permission.CONTAINER_VIEW,
                Permission.CONTAINER_MANAGE,
                Permission.GPU_VIEW,
                Permission.MONITORING_VIEW,
                Permission.MODEL_VIEW,
            }
        ),
    ),
    Role.AGENT_ADMIN: (
        "Manages agents, skills, tools and MCP servers",
        frozenset(
            {
                Permission.AGENT_VIEW,
                Permission.AGENT_CREATE,
                Permission.AGENT_EDIT,
                Permission.AGENT_EXECUTE,
                Permission.AGENT_DELETE,
                Permission.SKILL_VIEW,
                Permission.SKILL_MANAGE,
                Permission.TOOL_VIEW,
                Permission.TOOL_EXECUTE,
                Permission.TOOL_MANAGE,
                Permission.TOOL_APPROVE,
                Permission.MCP_VIEW,
                Permission.MCP_MANAGE,
                Permission.KNOWLEDGE_VIEW,
                Permission.MODEL_VIEW,
                Permission.MODEL_INFERENCE,
                Permission.TRACE_VIEW,
            }
        ),
    ),
    Role.DEVELOPER: (
        "Consumes platform APIs and manages own API keys",
        frozenset(
            {
                Permission.MODEL_INFERENCE,
                Permission.AGENT_EXECUTE,
                Permission.APIKEY_VIEW,
                Permission.APIKEY_MANAGE,
                Permission.USAGE_VIEW,
                Permission.DOCUMENT_UPLOAD,
            }
        )
        | _VIEWER,
    ),
    Role.USER: (
        "Chat and agent usage",
        frozenset(
            {
                Permission.MODEL_INFERENCE,
                Permission.AGENT_EXECUTE,
            }
        )
        | _VIEWER,
    ),
    Role.AUDITOR: (
        "Read-only access to audit logs and monitoring",
        frozenset(
            {
                Permission.AUDIT_VIEW,
                Permission.MONITORING_VIEW,
                Permission.TRACE_VIEW,
                Permission.USAGE_VIEW,
                Permission.USER_VIEW,
                Permission.INFRASTRUCTURE_VIEW,
            }
        )
        | _VIEWER,
    ),
}


def split_permission(name: str) -> tuple[str, str]:
    """Split ``"model.deploy"`` into ``("model", "deploy")``."""
    resource, _, action = name.partition(".")
    if not resource or not action:
        raise ValueError(f"Permission {name!r} must be of the form 'resource.action'")
    return resource, action
