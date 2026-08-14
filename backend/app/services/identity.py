"""End-user attribution behind a shared frontend (M17).

The problem this solves: Open WebUI holds **one** API key on behalf of every person in
the organisation. Without something more, every chat in the building accounts to a single
identity, and "who used how many tokens" is unanswerable — which is exactly the question
a chargeback report, a quota, or an audit of who asked the model what needs to answer.

The frontend therefore forwards the signed-in user alongside the request. That opens the
obvious hole: a forwarded identity is just a string in a header, so anything holding an
API key could attribute its traffic to anyone. Three mechanisms, strongest first:

1. **Signed JWT** (`X-OpenWebUI-User-Jwt`). The client and the platform share an HS256
   secret; the platform verifies signature, issuer and expiry. Stealing the API key is
   not enough to forge an identity — you need the secret too. This is what the platform's
   own Open WebUI deployment uses.
2. **Plaintext header**, accepted only from a client the operator explicitly marked
   trusted, and only when that client has no signing secret. Weaker: anyone holding that
   client's key can assert anything. Supported because not every frontend can sign.
3. **The OpenAI-standard `user` body field.** Self-reported by definition — OpenAI
   defines it as an opaque end-user identifier for abuse monitoring. Recorded, because
   applications genuinely use it for per-tenant breakdown, but never marked trusted.

Only 1 and 2 set ``trusted``. A report that mixed 3 in with them would bill people for
traffic anyone could have labelled as theirs.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from starlette.datastructures import Headers

from app.config.settings import GatewaySettings
from app.core.logging import get_logger
from app.core.security import SecretCipher
from app.models.models_registry import ApiClient

log = get_logger(__name__)

#: Identity values are stored in a VARCHAR(255); a forwarded header is attacker-shaped
#: input, so it is truncated rather than allowed to fail the insert.
MAX_LENGTH = 255


@dataclass(frozen=True, slots=True)
class EndUser:
    subject: str
    trusted: bool


def resolve_forwarded_identity(
    headers: Headers,
    client: ApiClient,
    settings: GatewaySettings,
    cipher: SecretCipher,
) -> EndUser | None:
    """Read the end user a trusted client says this request is for.

    Returns ``None`` when the client may not assert an identity, or asserts none. A
    malformed or expired assertion also returns ``None`` rather than raising: the request
    is legitimate and answering it matters more than attributing it, so it is served and
    accounted to the client instead. The failure is logged, because a persistent one
    means attribution is silently degrading.
    """
    if not client.trusted_identity_headers:
        return None

    secret = _signing_secret(client, cipher)
    if secret is not None:
        # Deliberately no plaintext fallback. Falling back would let anyone bypass the
        # signature simply by omitting the signed header.
        return _from_jwt(headers, settings, secret, client_name=client.name)

    for header in settings.identity_headers:
        value = headers.get(header)
        if value and value.strip():
            return EndUser(subject=value.strip()[:MAX_LENGTH], trusted=True)
    return None


def self_reported_identity(body: dict[str, object]) -> EndUser | None:
    """The request's OpenAI-standard `user` field. Never trusted."""
    value = body.get("user")
    if isinstance(value, str) and value.strip():
        return EndUser(subject=value.strip()[:MAX_LENGTH], trusted=False)
    return None


def _signing_secret(client: ApiClient, cipher: SecretCipher) -> str | None:
    if not client.identity_jwt_secret_encrypted:
        return None
    try:
        return cipher.decrypt(client.identity_jwt_secret_encrypted)
    except Exception:
        # A secret that will not decrypt means the master key changed. Refusing every
        # assertion is the safe outcome — the alternative is falling through to plaintext,
        # which would quietly downgrade a signed integration to an unsigned one.
        log.exception("identity_secret_undecryptable", client=client.name)
        return None


def _from_jwt(
    headers: Headers, settings: GatewaySettings, secret: str, *, client_name: str
) -> EndUser | None:
    token = headers.get(settings.identity_jwt_header)
    if not token:
        return None

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer=settings.identity_jwt_issuer,
            # `exp` is verified by default; requiring it makes a token minted without one
            # invalid rather than eternally valid.
            options={"require": ["exp", "iss"]},
        )
    except jwt.InvalidTokenError as exc:
        log.warning(
            "identity_jwt_rejected",
            client=client_name,
            reason=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None

    for claim in settings.identity_jwt_claims:
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            return EndUser(subject=value.strip()[:MAX_LENGTH], trusted=True)

    log.warning("identity_jwt_no_subject", client=client_name, claims=sorted(claims))
    return None
