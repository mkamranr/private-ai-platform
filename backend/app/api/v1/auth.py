"""Authentication endpoints (M03, §8).

``POST /auth/login``, ``/auth/refresh``, ``/auth/logout``, ``GET /auth/me``, plus provider
discovery and the OIDC redirect flow.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import AuthProvidersDep, AuthServiceDep, CurrentUserDep, RedisDep, SettingsDep
from app.core.errors import AuthenticationError, NotFoundError
from app.core.interfaces.auth_provider import AuthProvider, RedirectAuthProvider
from app.schemas.auth import (
    AuthProviderRead,
    CurrentUserRead,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse

#: OIDC state lives in Redis rather than a cookie so it works across control-plane
#: replicas, and expires on its own if a sign-in is abandoned half-way.
_STATE_PREFIX = "oidc:state:"
_STATE_TTL_SECONDS = 600

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/login", response_model=TokenResponse, summary="Exchange credentials for tokens")
async def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthServiceDep,
) -> TokenResponse:
    """Authenticate and issue an access/refresh pair.

    Both success and failure are audited (§M24). Failures are written on an
    independent transaction, since this request is about to raise and roll back —
    otherwise the platform would log successful logins and silently drop every
    failed one.
    """
    pair = await auth.login(
        payload.username,
        payload.password,
        user_agent=request.headers.get("user-agent"),
    )
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


@router.post("/refresh", response_model=TokenResponse, summary="Refresh an access token")
async def refresh(payload: RefreshRequest, auth: AuthServiceDep) -> TokenResponse:
    pair = await auth.refresh(payload.refresh_token)
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


@router.post("/logout", response_model=MessageResponse, summary="Record a logout")
async def logout(user: CurrentUserDep, auth: AuthServiceDep) -> MessageResponse:
    """Audit the logout and instruct the client to discard its tokens.

    Server-side invalidation needs a revocation store keyed on the token's ``jti``
    claim, which Phase 6 adds along with the rest of the enterprise auth work.
    """
    await auth.logout(user)
    return MessageResponse(message="Logged out. Discard your tokens.")


@router.get("/me", response_model=CurrentUserRead, summary="The authenticated user")
async def me(user: CurrentUserDep) -> CurrentUserRead:
    """Identity plus resolved permissions.

    The permission list lets the admin UI hide controls the user cannot use. It is
    presentation only — every endpoint re-checks server-side (§M03).
    """
    return CurrentUserRead.model_validate(
        {
            **{
                field: getattr(user, field)
                for field in (
                    "id",
                    "username",
                    "email",
                    "full_name",
                    "is_active",
                    "is_superuser",
                    "last_login_at",
                    "created_at",
                )
            },
            "roles": user.roles,
            # A superuser bypasses checks entirely, so enumerating grants would
            # understate what they can do; the flag above is the honest signal.
            "permissions": sorted(user.effective_permissions),
        }
    )


# ---------------------------------------------------------------------------
# Providers and the OIDC redirect flow (M03, Phase 6)
# ---------------------------------------------------------------------------
@router.get(
    "/providers",
    response_model=list[AuthProviderRead],
    summary="How this platform lets people sign in",
)
async def list_providers(providers: AuthProvidersDep) -> list[AuthProviderRead]:
    """Unauthenticated on purpose: the sign-in page has to render before anyone has a token.

    It exposes only which mechanisms exist, never whether a given account uses one — that
    would be the user-enumeration oracle the login endpoint is careful not to be.
    """
    return [
        AuthProviderRead(
            name=p.name,
            display_name=p.display_name,
            kind=p.kind,
            start_url=f"/api/v1/auth/{p.name}/authorize" if p.kind == "REDIRECT" else None,
        )
        for p in providers
    ]


@router.get(
    "/oidc/authorize",
    summary="Begin single sign-on",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
async def oidc_authorize(
    request: Request,
    redis: RedisDep,
    settings: SettingsDep,
    providers: AuthProvidersDep,
) -> RedirectResponse:
    """Redirect the browser to the identity provider.

    The ``state`` is minted here and stored in Redis with a short TTL, then required to
    match on the way back. Without that, an attacker can feed a victim's browser a code
    they obtained themselves and have the platform issue tokens for the attacker's
    account — login CSRF.
    """
    provider = _redirect_provider(providers)
    state = secrets.token_urlsafe(32)
    redirect_uri = _callback_url(request, settings)
    await redis.set(f"{_STATE_PREFIX}{state}", redirect_uri, ex=_STATE_TTL_SECONDS)
    return RedirectResponse(
        await provider.authorization_url(state=state, redirect_uri=redirect_uri),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.get("/oidc/callback", response_model=TokenResponse, summary="Finish single sign-on")
async def oidc_callback(
    request: Request,
    auth: AuthServiceDep,
    redis: RedisDep,
    providers: AuthProvidersDep,
    code: str = Query(..., description="Authorization code from the identity provider."),
    state: str = Query(..., description="Must match the value minted by /oidc/authorize."),
) -> TokenResponse:
    provider = _redirect_provider(providers)

    # Deleted as it is read, so a state cannot be replayed. `getdel` makes that atomic —
    # a get-then-delete would let two concurrent callbacks both succeed.
    redirect_uri = await redis.getdel(f"{_STATE_PREFIX}{state}")
    if redirect_uri is None:
        raise AuthenticationError("This sign-in link has expired or was already used. Start again.")
    if isinstance(redirect_uri, bytes):
        redirect_uri = redirect_uri.decode()

    identity = await provider.exchange(code=code, redirect_uri=redirect_uri)
    pair = await auth.login_external(identity, user_agent=request.headers.get("user-agent"))
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


def _redirect_provider(providers: list[AuthProvider]) -> RedirectAuthProvider:
    for provider in providers:
        if isinstance(provider, RedirectAuthProvider):
            return provider
    raise NotFoundError(
        "Single sign-on is not configured on this platform. Set OIDC__ENABLED=true and "
        "restart the control plane."
    )


def _callback_url(request: Request, settings: SettingsDep) -> str:
    """Where the IdP sends the browser back.

    Derived from the incoming request rather than configured, so a platform reached by
    several names still returns to the one the person actually used — and so the value
    registered at the IdP is the only place this has to be kept in step. It must match
    byte-for-byte between authorize and callback, which is why it is stored with the
    state rather than recomputed.
    """
    base = settings.platform.public_base_url or str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/api/v1/auth/oidc/callback"
