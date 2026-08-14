"""Pydantic request/response schemas.

Kept separate from ORM models on purpose: a schema is a wire contract, a model is
a storage layout, and letting one serve both is how password hashes end up in API
responses.
"""

from app.schemas.auth import (
    CurrentUserRead,
    LoginRequest,
    PermissionRead,
    RefreshRequest,
    RoleRead,
    RoleSummary,
    TokenResponse,
    UserActiveUpdateRequest,
    UserCreateRequest,
    UserRead,
    UserRolesUpdateRequest,
)
from app.schemas.common import ErrorResponse, MessageResponse, ORMModel, Page
from app.schemas.health import (
    DependencyStatus,
    HealthResponse,
    LivenessResponse,
    VersionResponse,
)

__all__ = [
    "CurrentUserRead",
    "DependencyStatus",
    "ErrorResponse",
    "HealthResponse",
    "LivenessResponse",
    "LoginRequest",
    "MessageResponse",
    "ORMModel",
    "Page",
    "PermissionRead",
    "RefreshRequest",
    "RoleRead",
    "RoleSummary",
    "TokenResponse",
    "UserActiveUpdateRequest",
    "UserCreateRequest",
    "UserRead",
    "UserRolesUpdateRequest",
    "VersionResponse",
]
