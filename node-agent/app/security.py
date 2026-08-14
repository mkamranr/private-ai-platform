"""Node agent authentication (M04, §25).

The agent can create containers and read host telemetry, so an unauthenticated agent
reachable on any network is a remote-execution primitive. Every endpoint except
`/health` requires a bearer token.

Transport security is layered on top rather than replacing this. §M04 requires the
control plane to reach agents over HTTPS, and on a remote host that means mTLS with the
platform's own CA (`scripts/gen_certs.sh`) — an air-gapped site has no reachable public
CA. Token auth still applies underneath, so a stolen client certificate alone is not
enough.
"""

from __future__ import annotations

import hmac

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings

log = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False, description="Node agent token")


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Reject anything without the configured token.

    Compared with :func:`hmac.compare_digest`, not ``==``: a short-circuiting
    comparison leaks the token prefix through response timing, and the agent is
    exactly the component an attacker on the management network would probe.
    """
    settings: Settings = request.app.state.settings
    expected = settings.auth_token.get_secret_value()

    supplied = credentials.credentials if credentials else ""
    if not supplied or not hmac.compare_digest(supplied, expected):
        peer = request.client.host if request.client else "unknown"
        log.warning(
            "agent_auth_failed",
            path=request.url.path,
            peer=peer,
            reason="missing token" if not supplied else "invalid token",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid node agent token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def validate_startup_config(settings: Settings) -> list[str]:
    """Return warnings about an insecure configuration.

    Logged loudly at startup rather than raising: an operator running locally must be
    able to start the agent without certificates, but must not be able to do so
    *without noticing* that it is unencrypted.
    """
    warnings: list[str] = []

    if len(settings.auth_token.get_secret_value()) < 32:
        warnings.append(
            "NODE_AGENT_AUTH_TOKEN is shorter than 32 characters. Generate one with "
            "`openssl rand -hex 32`."
        )
    if not settings.tls_enabled:
        warnings.append(
            "TLS is disabled. Acceptable only when the agent is reachable solely from "
            "a private container network. A remote node MUST set "
            "NODE_AGENT_TLS_CERTFILE/KEYFILE/CA_CERTS (§M04)."
        )
    elif not settings.tls_ca_certs:
        warnings.append(
            "TLS is enabled without a CA bundle, so client certificates cannot be "
            "verified. Set NODE_AGENT_TLS_CA_CERTS for mutual TLS."
        )
    if settings.allow_unmanaged_control:
        warnings.append(
            "NODE_AGENT_ALLOW_UNMANAGED_CONTROL is on. The agent will stop and remove "
            "containers it did not create, including this host's own infrastructure."
        )
    if settings.gpu_probe == "fake":
        warnings.append(
            "GPU probe is 'fake'. This node reports SYNTHETIC GPU telemetry and must "
            "never be used to schedule real inference."
        )
    return warnings


__all__ = ["require_token", "validate_startup_config"]
