"""Authentication endpoints (M03, §8).

``POST /auth/login``, ``/auth/refresh``, ``/auth/logout``, ``GET /auth/me``, plus provider
discovery and the OIDC redirect flow.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import (
    AuthProvidersDep,
    AuthServiceDep,
    CurrentUserDep,
    EffectiveSettingsDep,
    RedisDep,
)
from app.api.routing import CommittingRoute
from app.config.settings import Settings
from app.core.errors import AuthenticationError, NotFoundError
from app.core.interfaces.auth_provider import AuthProvider, RedirectAuthProvider
from app.schemas.auth import (
    AuthProviderRead,
    CurrentUserRead,
    LoginRequest,
    LogoutRequest,
    MfaChallengeResponse,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaEnabledResponse,
    MfaPasswordRequest,
    MfaSetupResponse,
    MfaStatusResponse,
    MfaTokenRequest,
    MfaVerifyRequest,
    RecoveryCodesResponse,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import MessageResponse
from app.services.auth import MfaChallenge, TokenPair
from app.services.mfa import MfaSetup

#: OIDC state lives in Redis rather than a cookie so it works across control-plane
#: replicas, and expires on its own if a sign-in is abandoned half-way.
_STATE_PREFIX = "oidc:state:"
_STATE_TTL_SECONDS = 600

router = APIRouter(prefix="/auth", tags=["authentication"], route_class=CommittingRoute)


def _tokens(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
    )


def _setup(setup: MfaSetup) -> MfaSetupResponse:
    return MfaSetupResponse(secret=setup.secret, otpauth_uri=setup.otpauth_uri, qr_svg=setup.qr_svg)


@router.post(
    "/login",
    response_model=TokenResponse | MfaChallengeResponse,
    summary="Exchange credentials for tokens",
)
async def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthServiceDep,
) -> TokenResponse | MfaChallengeResponse:
    """Authenticate and issue an access/refresh pair.

    Both success and failure are audited (§M24). Failures are written on an
    independent transaction, since this request is about to raise and roll back —
    otherwise the platform would log successful logins and silently drop every
    failed one.
    """
    result = await auth.login(
        payload.username,
        payload.password,
        user_agent=request.headers.get("user-agent"),
    )
    if isinstance(result, MfaChallenge):
        # E15: the password was right; the authenticator code is the next step.
        return MfaChallengeResponse(
            mfa_token=result.mfa_token,
            enrolment_required=result.enrolment_required,
            expires_in=result.expires_in,
        )
    return _tokens(result)


# -- two-factor authentication (E15) ---------------------------------------------
@router.post("/mfa/verify", response_model=TokenResponse, summary="Sign in: the code step")
async def mfa_verify(
    payload: MfaVerifyRequest, request: Request, auth: AuthServiceDep
) -> TokenResponse:
    """Exchange the step token from ``/auth/login`` and a code for tokens.

    The code is six digits from the authenticator app, or an unused recovery code. A
    wrong code counts toward the account lockout, like a wrong password.
    """
    pair = await auth.complete_mfa(
        payload.mfa_token, payload.code, user_agent=request.headers.get("user-agent")
    )
    return _tokens(pair)


@router.post(
    "/mfa/enrol/start",
    response_model=MfaSetupResponse,
    summary="Sign in: set up the required authenticator",
)
async def mfa_enrol_start(payload: MfaTokenRequest, auth: AuthServiceDep) -> MfaSetupResponse:
    """For an account the policy obliges to use MFA: a new secret and its QR code."""
    return _setup(await auth.start_enrolment(payload.mfa_token))


@router.post(
    "/mfa/enrol/finish",
    response_model=MfaEnabledResponse,
    summary="Sign in: confirm the authenticator",
)
async def mfa_enrol_finish(
    payload: MfaVerifyRequest, request: Request, auth: AuthServiceDep
) -> MfaEnabledResponse:
    """The first code turns MFA on and signs in. Recovery codes are returned once."""
    pair, codes = await auth.finish_enrolment(
        payload.mfa_token, payload.code, user_agent=request.headers.get("user-agent")
    )
    return MfaEnabledResponse(**_tokens(pair).model_dump(), recovery_codes=codes)


@router.get("/mfa", response_model=MfaStatusResponse, summary="Two-factor status")
async def mfa_status(user: CurrentUserDep, auth: AuthServiceDep) -> MfaStatusResponse:
    return MfaStatusResponse(
        available=user.auth_provider == "local",
        enabled=user.mfa_enabled,
        enabled_at=user.mfa_enabled_at,
        recovery_codes_left=user.mfa_recovery_codes_left,
        required=auth.mfa_required(user),
    )


@router.post("/mfa/setup", response_model=MfaSetupResponse, summary="Start setting up MFA")
async def mfa_setup(
    payload: MfaPasswordRequest, user: CurrentUserDep, auth: AuthServiceDep
) -> MfaSetupResponse:
    """A new secret and its QR code. Nothing changes at sign-in until it is confirmed."""
    return _setup(auth.begin_mfa(user, payload.password))


@router.post("/mfa/enable", response_model=MfaEnabledResponse, summary="Turn MFA on")
async def mfa_enable(
    payload: MfaCodeRequest, user: CurrentUserDep, auth: AuthServiceDep
) -> MfaEnabledResponse:
    """Confirm with a first code. Other sessions are signed out; use the new tokens."""
    pair, codes = await auth.enable_mfa(user, payload.code)
    return MfaEnabledResponse(**_tokens(pair).model_dump(), recovery_codes=codes)


@router.post("/mfa/disable", response_model=MessageResponse, summary="Turn MFA off")
async def mfa_disable(
    payload: MfaDisableRequest, user: CurrentUserDep, auth: AuthServiceDep
) -> MessageResponse:
    """Needs the password and a code. Refused while the policy requires MFA."""
    await auth.disable_mfa(user, payload.password, payload.code)
    return MessageResponse(message="Two-factor authentication is off.")


@router.post(
    "/mfa/recovery-codes",
    response_model=RecoveryCodesResponse,
    summary="Replace the recovery codes",
)
async def mfa_recovery_codes(
    payload: MfaCodeRequest, user: CurrentUserDep, auth: AuthServiceDep
) -> RecoveryCodesResponse:
    """New codes; the old ones stop working. Needs a current code."""
    return RecoveryCodesResponse(
        recovery_codes=await auth.regenerate_recovery_codes(user, payload.code)
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


_bearer = HTTPBearer(auto_error=False)


@router.post("/logout", response_model=MessageResponse, summary="Sign out this session")
async def logout(
    user: CurrentUserDep,
    auth: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    payload: LogoutRequest | None = None,
) -> MessageResponse:
    """Revoke this access token, and the refresh token if the body carries it (E14).

    Both are refused from now on, even though they have not expired. Audited.
    """
    await auth.logout(
        user,
        access_token=credentials.credentials if credentials else None,
        refresh_token=payload.refresh_token if payload else None,
    )
    return MessageResponse(message="Signed out. This session's tokens no longer work.")


@router.post("/logout-all", response_model=MessageResponse, summary="Sign out every session")
async def logout_all(user: CurrentUserDep, auth: AuthServiceDep) -> MessageResponse:
    """Every token issued to this account stops working, including this one (E14)."""
    await auth.logout_everywhere(user)
    return MessageResponse(message="Signed out everywhere. Sign in again to continue.")


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
                    "auth_provider",
                    "mfa_enabled",
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
    settings: EffectiveSettingsDep,
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


def _callback_url(request: Request, settings: Settings) -> str:
    """Where the IdP sends the browser back.

    Derived from the incoming request rather than configured, so a platform reached by
    several names still returns to the one the person actually used — and so the value
    registered at the IdP is the only place this has to be kept in step. It must match
    byte-for-byte between authorize and callback, which is why it is stored with the
    state rather than recomputed.
    """
    base = settings.platform.public_base_url or str(request.base_url).rstrip("/")
    return f"{base.rstrip('/')}/api/v1/auth/oidc/callback"
