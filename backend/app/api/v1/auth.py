"""Authentication endpoints (M03, §8).

``POST /auth/login``, ``/auth/refresh``, ``/auth/logout``, ``GET /auth/me``, plus provider
discovery and the OIDC redirect flow.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import (
    AuthProvidersDep,
    AuthServiceDep,
    CurrentUserDep,
    EffectiveSettingsDep,
    PasskeyServiceDep,
    RedisDep,
    TrustedDeviceServiceDep,
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
    PasskeyOptionsResponse,
    PasskeyRead,
    PasskeyRegisterRequest,
    PasskeySignInRequest,
    PasskeyVerifyRequest,
    RecoveryCodesResponse,
    RefreshRequest,
    TokenResponse,
    TrustedDeviceRead,
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
        device_token=pair.device_token,
        device_expires_at=pair.device_expires_at,
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
        device_token=payload.device_token,
        challenge=payload.challenge.model_dump() if payload.challenge else None,
    )
    if isinstance(result, MfaChallenge):
        # E15: the password was right; the authenticator code is the next step.
        return MfaChallengeResponse(
            mfa_token=result.mfa_token,
            enrolment_required=result.enrolment_required,
            expires_in=result.expires_in,
            passkey_available=result.passkey_available,
            remember_days=result.remember_days,
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
        payload.mfa_token,
        payload.code,
        user_agent=request.headers.get("user-agent"),
        remember_device=payload.remember_device,
    )
    return _tokens(pair)


# -- passkeys and trusted browsers (E20) -------------------------------------------
@router.post(
    "/mfa/passkey/options",
    response_model=PasskeyOptionsResponse,
    summary="Sign in: a passkey challenge",
)
async def mfa_passkey_options(
    payload: MfaTokenRequest, auth: AuthServiceDep
) -> PasskeyOptionsResponse:
    """For the two-factor step: options for ``navigator.credentials.get``."""
    options = await auth.passkey_options(payload.mfa_token)
    return PasskeyOptionsResponse(
        public_key=options.public_key, challenge_token=options.challenge_token
    )


@router.post(
    "/mfa/passkey/verify", response_model=TokenResponse, summary="Sign in: the passkey step"
)
async def mfa_passkey_verify(
    payload: PasskeyVerifyRequest, request: Request, auth: AuthServiceDep
) -> TokenResponse:
    """Finish the two-factor step with a passkey. A failure counts like a wrong code."""
    pair = await auth.complete_passkey(
        payload.mfa_token,
        challenge_token=payload.challenge_token,
        credential=payload.credential,
        user_agent=request.headers.get("user-agent"),
        remember_device=payload.remember_device,
    )
    return _tokens(pair)


# -- passwordless sign-in (E30) --------------------------------------------------
@router.post(
    "/passkey/options",
    response_model=PasskeyOptionsResponse,
    summary="Sign in with a passkey: the challenge",
)
async def passkey_signin_options(auth: AuthServiceDep) -> PasskeyOptionsResponse:
    """Options for ``navigator.credentials.get`` with no account named: the browser
    offers the passkeys it holds for this site."""
    options = await auth.passkey_signin_options()
    return PasskeyOptionsResponse(
        public_key=options.public_key, challenge_token=options.challenge_token
    )


@router.post("/passkey/verify", response_model=TokenResponse, summary="Sign in with a passkey")
async def passkey_signin(
    payload: PasskeySignInRequest, request: Request, auth: AuthServiceDep
) -> TokenResponse:
    """Sign in with a passkey alone: no password, and no code step, because the passkey
    was unlocked on the device (user verification is required)."""
    pair = await auth.passkey_signin(
        challenge_token=payload.challenge_token,
        credential=payload.credential,
        user_agent=request.headers.get("user-agent"),
    )
    return _tokens(pair)


@router.get("/passkeys", response_model=list[PasskeyRead], summary="Your passkeys")
async def list_passkeys(user: CurrentUserDep, passkeys: PasskeyServiceDep) -> list[PasskeyRead]:
    return [PasskeyRead.model_validate(p) for p in await passkeys.list_for(user)]


@router.post(
    "/passkeys/options",
    response_model=PasskeyOptionsResponse,
    summary="Start adding a passkey",
)
async def passkey_registration_options(
    payload: MfaPasswordRequest, user: CurrentUserDep, auth: AuthServiceDep
) -> PasskeyOptionsResponse:
    """Needs the password, and two-factor authentication already on."""
    options = await auth.passkey_registration_options(user, payload.password)
    return PasskeyOptionsResponse(
        public_key=options.public_key, challenge_token=options.challenge_token
    )


@router.post(
    "/passkeys",
    response_model=PasskeyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a passkey",
)
async def register_passkey(
    payload: PasskeyRegisterRequest, user: CurrentUserDep, passkeys: PasskeyServiceDep
) -> PasskeyRead:
    passkey = await passkeys.register(
        user,
        challenge_token=payload.challenge_token,
        credential=payload.credential,
        name=payload.name,
    )
    return PasskeyRead.model_validate(passkey)


@router.delete("/passkeys/{passkey_id}", response_model=MessageResponse, summary="Remove a passkey")
async def remove_passkey(
    passkey_id: uuid.UUID, user: CurrentUserDep, passkeys: PasskeyServiceDep
) -> MessageResponse:
    await passkeys.remove(user, passkey_id)
    return MessageResponse(message="Passkey removed.")


@router.get(
    "/trusted-devices", response_model=list[TrustedDeviceRead], summary="Your trusted browsers"
)
async def list_trusted_devices(
    user: CurrentUserDep, devices: TrustedDeviceServiceDep
) -> list[TrustedDeviceRead]:
    return [TrustedDeviceRead.model_validate(d) for d in await devices.list_for(user)]


@router.delete(
    "/trusted-devices/{device_id}", response_model=MessageResponse, summary="Forget a browser"
)
async def forget_trusted_device(
    device_id: uuid.UUID, user: CurrentUserDep, devices: TrustedDeviceServiceDep
) -> MessageResponse:
    await devices.forget(user, device_id)
    return MessageResponse(message="That browser will ask for the second step again.")


@router.delete(
    "/trusted-devices", response_model=MessageResponse, summary="Forget every trusted browser"
)
async def forget_all_trusted_devices(
    user: CurrentUserDep, devices: TrustedDeviceServiceDep
) -> MessageResponse:
    count = await devices.forget_all(user)
    return MessageResponse(message=f"Forgot {count} trusted browser(s).")


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
async def mfa_status(
    user: CurrentUserDep,
    auth: AuthServiceDep,
    passkeys: PasskeyServiceDep,
    devices: TrustedDeviceServiceDep,
    effective: EffectiveSettingsDep,
) -> MfaStatusResponse:
    return MfaStatusResponse(
        available=user.auth_provider == "local",
        enabled=user.mfa_enabled,
        enabled_at=user.mfa_enabled_at,
        recovery_codes_left=user.mfa_recovery_codes_left,
        required=auth.mfa_required(user),
        passkeys_supported=passkeys.relying_party is not None,
        passkeys=await passkeys.count_for(user),
        trusted_devices=len(await devices.list_for(user)),
        remember_days=effective.auth.mfa_remember_days,
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
                    "approval_reminder_emails",
                    "dashboard_window_hours",
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
