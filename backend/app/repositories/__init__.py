"""Repositories — the only layer that writes SQL (Rule 6)."""

from app.repositories.audit import AuditRepository
from app.repositories.base import BaseRepository
from app.repositories.infrastructure import (
    ContainerRepository,
    GpuAllocationRepository,
    GpuHealthEventRepository,
    GpuMetricRepository,
    GpuProcessRepository,
    GpuRepository,
    NodeRepository,
)
from app.repositories.models_registry import (
    ApiClientRepository,
    ApiKeyRepository,
    ModelAliasRepository,
    ModelDeploymentRepository,
    ModelRepository,
    UsageRepository,
)
from app.repositories.system import SystemSettingRepository
from app.repositories.user import PermissionRepository, RoleRepository, UserRepository

__all__ = [
    "ApiClientRepository",
    "ApiKeyRepository",
    "AuditRepository",
    "BaseRepository",
    "ContainerRepository",
    "GpuAllocationRepository",
    "GpuHealthEventRepository",
    "GpuMetricRepository",
    "GpuProcessRepository",
    "GpuRepository",
    "ModelAliasRepository",
    "ModelDeploymentRepository",
    "ModelRepository",
    "NodeRepository",
    "PermissionRepository",
    "RoleRepository",
    "SystemSettingRepository",
    "UsageRepository",
    "UserRepository",
]
