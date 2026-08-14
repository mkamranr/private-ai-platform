"""Password hashing, JWT issuance/verification and secret encryption (M03, M24).

Choices worth recording:

* **argon2id** via ``argon2-cffi`` directly, not ``passlib``. passlib is
  effectively unmaintained and its bcrypt backend detection breaks against
  current bcrypt releases; argon2-cffi is maintained and argon2id is the current
  OWASP first choice.
* **PyJWT**, not python-jose, which has seen little maintenance.
* **Fernet** for tool credentials at rest. The air-gapped stack has no Vault, so
  credentials live in Postgres encrypted under a key mounted from outside the
  database — a database dump alone then yields nothing usable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from typing import Any, Final, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.config.settings import AuthSettings, SecuritySettings
from app.core.errors import TokenError

TokenType = Literal["access", "refresh"]

_API_KEY_PREFIX: Final = "aip"
_API_KEY_SECRET_BYTES: Final = 32
# A distinct prefix so a leaked enrolment token is identifiable on sight and cannot
# be mistaken for an API key in a log or a ticket.
_ENROLLMENT_PREFIX: Final = "aine"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
class PasswordHasherService:
    """argon2id hashing with parameters from configuration."""

    def __init__(self, settings: SecuritySettings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, hashed: str, password: str) -> bool:
        """Constant-time-ish verification. Returns False rather than raising."""
        try:
            return self._hasher.verify(hashed, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, hashed: str) -> bool:
        """True when a stored hash predates the current cost parameters."""
        try:
            return self._hasher.check_needs_rehash(hashed)
        except InvalidHashError:
            return True


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
class TokenService:
    """Issues and verifies the platform's own JWTs.

    Phase 6 adds an external identity provider (OIDC/Keycloak) behind an
    auth-provider interface; these tokens remain the platform's internal
    representation either way.
    """

    def __init__(self, settings: AuthSettings) -> None:
        self._secret = settings.jwt_secret_key.get_secret_value()
        self._algorithm = settings.jwt_algorithm
        self._issuer = settings.jwt_issuer
        self._access_ttl = settings.access_token_ttl_seconds
        self._refresh_ttl = settings.refresh_token_ttl_seconds

    def _encode(self, subject: str, token_type: TokenType, ttl: int, **claims: Any) -> str:
        now = dt.datetime.now(dt.UTC)
        payload: dict[str, Any] = {
            "sub": subject,
            "typ": token_type,
            "iss": self._issuer,
            "iat": now,
            "nbf": now,
            "exp": now + dt.timedelta(seconds=ttl),
            # Unique id per token, so Phase 6 can add a revocation list keyed on it.
            "jti": secrets.token_urlsafe(16),
            **claims,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def create_access_token(self, subject: str, **claims: Any) -> str:
        return self._encode(subject, "access", self._access_ttl, **claims)

    def create_refresh_token(self, subject: str, **claims: Any) -> str:
        return self._encode(subject, "refresh", self._refresh_ttl, **claims)

    def decode(self, token: str, *, expected_type: TokenType = "access") -> dict[str, Any]:
        """Verify signature, expiry and issuer. Raises :class:`TokenError`."""
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "typ"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("The token has expired.") from exc
        except jwt.InvalidTokenError as exc:
            # Covers bad signature, wrong issuer, malformed payload, missing claims.
            raise TokenError() from exc

        # A refresh token must not be usable as an access token.
        if payload.get("typ") != expected_type:
            raise TokenError(f"Expected a {expected_type} token.")
        return payload

    @property
    def access_ttl_seconds(self) -> int:
        return self._access_ttl


# ---------------------------------------------------------------------------
# Secret encryption at rest
# ---------------------------------------------------------------------------
class SecretCipher:
    """Fernet envelope encryption for credentials stored in Postgres.

    Used from Phase 4 for MCP/tool credentials. Constructed in Phase 0 so a
    misconfigured key fails at startup, not the first time a tool is registered.
    """

    def __init__(self, settings: SecuritySettings) -> None:
        key = settings.encryption_key.get_secret_value().encode()
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "SECURITY__ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "Could not decrypt stored secret — the encryption key has changed "
                "or the value is corrupt."
            ) from exc


# ---------------------------------------------------------------------------
# API keys (M20; issued in Phase 6, hashing defined here)
# ---------------------------------------------------------------------------
def _mint_prefixed_token(namespace: str) -> tuple[str, str, str]:
    """``(full, prefix, sha256)`` for a namespaced, hash-stored credential."""
    secret = secrets.token_urlsafe(_API_KEY_SECRET_BYTES)
    prefix = secrets.token_hex(4)
    full = f"{namespace}_{prefix}_{secret}"
    return full, prefix, hash_api_key(full)


def generate_api_key() -> tuple[str, str, str]:
    """Mint an API key.

    Returns ``(full_key, prefix, sha256_hash)``. Only the prefix and hash are
    stored; the full key is shown to the developer exactly once. Storing a
    reversible key would make a database read equivalent to holding every
    credential.
    """
    return _mint_prefixed_token(_API_KEY_PREFIX)


def generate_enrollment_token() -> tuple[str, str, str]:
    """Mint a node enrolment token (M04).

    Same shape and the same storage rule as an API key: the platform **verifies** what a
    node presents, so a one-way hash is enough and a database dump yields nothing usable.

    That is the mirror image of the *agent* token, which is Fernet-encrypted rather than
    hashed because the platform **presents** it to the agent on every poll — see the
    comment on ``Node.agent_token_encrypted``. Getting these two the wrong way round is
    the easy mistake here, so they are deliberately named and documented as a pair.

    Its own function rather than a parameter on :func:`generate_api_key`: calling "generate
    an API key" to mint an enrolment token is a small lie that would read wrong at the call
    site for ever.
    """
    return _mint_prefixed_token(_ENROLLMENT_PREFIX)


def hash_api_key(full_key: str) -> str:
    """SHA-256 of an API key.

    A fast hash is correct here, unlike for passwords: the key is 256 bits of
    machine-generated entropy, so there is nothing to brute-force, and gateway
    auth must not pay an argon2 cost on every request.
    """
    return hashlib.sha256(full_key.encode()).hexdigest()


def verify_api_key(full_key: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(full_key), stored_hash)
