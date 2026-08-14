"""Services — where behaviour lives (Rule 6).

A service depends on repositories and other services, never on FastAPI. That is
what lets the same service be driven from an HTTP route, an APScheduler worker or
the CLI, which matters from Phase 1 onward when metric collection and deployment
state machines run outside any request.
"""

from app.services.audit import AuditService
from app.services.auth import AuthService, TokenPair
from app.services.health import HealthService
from app.services.seed import SeedService
from app.services.user import UserService

__all__ = [
    "AuditService",
    "AuthService",
    "HealthService",
    "SeedService",
    "TokenPair",
    "UserService",
]
