"""ORM models.

Every model must be imported here. Alembic's autogenerate compares
``Base.metadata`` against the live database, and a model that is never imported is
absent from that metadata — so autogenerate would silently emit a migration that
DROPS its table. Adding a module and forgetting this line is the single most
destructive mistake available in this codebase.

Phase 0 owns seven tables. Each later module adds its own here as it lands.
"""

from app.models.agents import (
    Agent,
    AgentRun,
    AgentRunEvent,
    AgentSkill,
    AgentTool,
    AgentVersion,
    ApprovalState,
    McpServer,
    RunState,
    Skill,
    Tool,
    ToolExecution,
)
from app.models.audit import AuditAction, AuditLog, AuditResult
from app.models.auth import Permission, Role, User, role_permissions, user_roles
from app.models.infrastructure import (
    Container,
    ContainerState,
    EnrollmentStatus,
    Gpu,
    GpuAllocation,
    GpuHealth,
    GpuHealthEvent,
    GpuMetric,
    GpuProcess,
    Node,
    NodeEnrollment,
    NodeRole,
    NodeStatus,
)
from app.models.knowledge import (
    Conversation,
    ConversationMessage,
    Document,
    DocumentChunk,
    DocumentStatus,
    EmbeddingModel,
    KnowledgeBase,
    MemoryEntry,
    MemoryLayer,
)
from app.models.models_registry import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    ApiClient,
    ApiKey,
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelFile,
    ModelStatus,
    ModelType,
    UsageRecord,
)
from app.models.system import SystemSetting
from app.models.voice import (
    TERMINAL_VOICE_STATES,
    VoiceEvent,
    VoiceMessage,
    VoiceSession,
    VoiceSessionState,
)

__all__ = [
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "TERMINAL_VOICE_STATES",
    "Agent",
    "AgentRun",
    "AgentRunEvent",
    "AgentSkill",
    "AgentTool",
    "AgentVersion",
    "ApiClient",
    "ApiKey",
    "ApprovalState",
    "AuditAction",
    "AuditLog",
    "AuditResult",
    "Container",
    "ContainerState",
    "Conversation",
    "ConversationMessage",
    "DeploymentState",
    "Document",
    "DocumentChunk",
    "DocumentStatus",
    "EmbeddingModel",
    "EnrollmentStatus",
    "Gpu",
    "GpuAllocation",
    "GpuHealth",
    "GpuHealthEvent",
    "GpuMetric",
    "GpuProcess",
    "KnowledgeBase",
    "McpServer",
    "MemoryEntry",
    "MemoryLayer",
    "Model",
    "ModelAlias",
    "ModelDeployment",
    "ModelFile",
    "ModelStatus",
    "ModelType",
    "Node",
    "NodeEnrollment",
    "NodeRole",
    "NodeStatus",
    "Permission",
    "Role",
    "RunState",
    "Skill",
    "SystemSetting",
    "Tool",
    "ToolExecution",
    "UsageRecord",
    "User",
    "VoiceEvent",
    "VoiceMessage",
    "VoiceSession",
    "VoiceSessionState",
    "role_permissions",
    "user_roles",
]
