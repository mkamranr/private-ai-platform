"""Authentication and user schemas (M03)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.core.validators import PlatformEmail
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=1024)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — an OAuth2 type name, not a credential
    expires_in: int = Field(description="Access token lifetime in seconds")


class MfaChallengeResponse(BaseModel):
    """``/auth/login`` when the password was right and a second step is needed (E15).

    ``enrolment_required``: the platform's policy obliges this account to use an
    authenticator app and it has none yet. Set one up with ``/auth/mfa/enrol/start`` and
    ``/auth/mfa/enrol/finish``. Otherwise send a code to ``/auth/mfa/verify``.
    """

    mfa_required: bool = True
    mfa_token: str
    enrolment_required: bool = False
    expires_in: int = Field(description="Seconds left to complete this step")


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=4096)
    #: Six digits from the authenticator app, or one unused recovery code.
    code: str = Field(min_length=6, max_length=32)


class MfaTokenRequest(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=4096)


class MfaPasswordRequest(BaseModel):
    """Setting up MFA on a signed-in session asks for the password again."""

    password: str = Field(min_length=1, max_length=1024)


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class MfaDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    code: str = Field(min_length=6, max_length=32)


class MfaSetupResponse(BaseModel):
    """What the authenticator app needs. The secret is shown for typing in by hand."""

    secret: str
    otpauth_uri: str
    qr_svg: str


class RecoveryCodesResponse(BaseModel):
    """Single-use codes for when the phone is lost. Shown once and never again."""

    recovery_codes: list[str]


class MfaEnabledResponse(TokenResponse):
    """MFA is on: new tokens (other sessions were signed out) and the recovery codes."""

    recovery_codes: list[str]


class MfaStatusResponse(BaseModel):
    available: bool
    enabled: bool
    enabled_at: dt.datetime | None = None
    recovery_codes_left: int = 0
    #: The platform's policy requires it for this account (so it cannot be turned off).
    required: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """Optional: the session's refresh token, so it is revoked too (E14)."""

    refresh_token: str | None = None


class PermissionRead(ORMModel):
    name: str
    resource: str
    action: str
    description: str | None = None


class RoleRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_system: bool
    permissions: list[PermissionRead] = Field(default_factory=list)


class RoleSummary(ORMModel):
    """Role without its permission list, for embedding in a user record."""

    id: uuid.UUID
    name: str
    description: str | None = None


class UserRead(ORMModel):
    id: uuid.UUID
    username: str
    email: str
    full_name: str | None = None
    is_active: bool
    is_superuser: bool
    last_login_at: dt.datetime | None = None
    created_at: dt.datetime
    roles: list[RoleSummary] = Field(default_factory=list)
    #: Which provider vouches for this account (M03). Shown because the difference is
    #: operationally load-bearing: a federated account's roles are overwritten from the
    #: directory on every sign-in, so editing them here is undone at the next login.
    auth_provider: str = "local"
    #: E14: wrong passwords since the last sign-in, and the lock they caused, if any.
    failed_login_count: int = 0
    locked_until: dt.datetime | None = None
    #: E15: whether sign-in asks this user for an authenticator code.
    mfa_enabled: bool = False
    # Never a password hash. Serialising one would leak it into every log,
    # response cache and browser history that touches this endpoint.


class CurrentUserRead(UserRead):
    """``/auth/me``: adds the resolved permission set.

    Returned so the admin UI can hide controls the user cannot use. It is a
    convenience for rendering only — authorisation is always re-checked
    server-side (§M03).
    """

    permissions: list[str] = Field(default_factory=list)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=150, pattern=r"^[A-Za-z0-9._-]+$")
    # PlatformEmail rather than EmailStr: internal domains like ai-platform.local must
    # be accepted, and DNS is unavailable on an air-gapped host. See core/validators.py.
    email: PlatformEmail
    password: str = Field(min_length=12, max_length=1024)
    full_name: str | None = Field(default=None, max_length=255)
    roles: list[str] = Field(default_factory=list)


class UserRolesUpdateRequest(BaseModel):
    roles: list[str]


class UserActiveUpdateRequest(BaseModel):
    is_active: bool


class PasswordSetRequest(BaseModel):
    """An administrator setting someone else's password."""

    password: str = Field(min_length=12, max_length=1024)


class PasswordChangeRequest(BaseModel):
    """Someone changing their own password.

    The current one is required even though the caller already holds a valid token: a
    token left behind on a shared workstation would otherwise be enough to take the
    account permanently.
    """

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class AuthProviderRead(BaseModel):
    """A sign-in mechanism this platform offers (M03).

    Carries no hint about which accounts use it. The sign-in page needs to know that a
    single-sign-on button should exist; it must not be able to ask whether a *particular*
    person would use it, which would be user enumeration by another route.
    """

    name: str = Field(description="Stable key, e.g. 'local', 'ldap', 'oidc'.")
    display_name: str = Field(description="Label for the sign-in page.")
    kind: str = Field(description="PASSWORD — post credentials here. REDIRECT — send the browser.")
    start_url: str | None = Field(
        default=None,
        description="Where to send the browser for a REDIRECT provider; null for PASSWORD.",
    )
