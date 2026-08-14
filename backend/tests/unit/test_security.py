"""Password hashing, JWT and secret encryption (M03, M24)."""

from __future__ import annotations

import datetime as dt

import jwt
import pytest

from app.config.settings import AuthSettings, SecuritySettings
from app.core.errors import TokenError
from app.core.security import (
    PasswordHasherService,
    SecretCipher,
    TokenService,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)


def _security(**overrides: object) -> SecuritySettings:
    """Build SecuritySettings through validation.

    ``model_copy`` bypasses validation, so a plain ``str`` passed for a
    ``SecretStr`` field stays a ``str`` and blows up later with
    ``'str' object has no attribute 'get_secret_value'``. Constructing afresh runs
    the validators and coerces properly.
    """
    base: dict[str, object] = {
        "encryption_key": "cHJpdmF0ZS1haS1wbGF0Zm9ybS10ZXN0LWtleS0wMDE=",
        "argon2_time_cost": 1,
        "argon2_memory_cost_kib": 8,
        "argon2_parallelism": 1,
    }
    return SecuritySettings(**{**base, **overrides})  # type: ignore[arg-type]


def _auth(**overrides: object) -> AuthSettings:
    base: dict[str, object] = {"jwt_secret_key": "t" * 32}
    return AuthSettings(**{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def security_settings() -> SecuritySettings:
    # Deliberately low argon2 cost: the production defaults make the suite crawl,
    # and these tests verify behaviour, not tuning.
    return _security()


@pytest.fixture
def auth_settings() -> AuthSettings:
    return _auth()


class TestPasswordHashing:
    def test_hash_then_verify(self, security_settings: SecuritySettings) -> None:
        hasher = PasswordHasherService(security_settings)
        hashed = hasher.hash("correct horse battery staple")
        assert hasher.verify(hashed, "correct horse battery staple") is True

    def test_wrong_password_rejected(self, security_settings: SecuritySettings) -> None:
        hasher = PasswordHasherService(security_settings)
        assert hasher.verify(hasher.hash("right"), "wrong") is False

    def test_hash_is_salted(self, security_settings: SecuritySettings) -> None:
        """Identical passwords must produce different hashes."""
        hasher = PasswordHasherService(security_settings)
        assert hasher.hash("same") != hasher.hash("same")

    def test_password_not_recoverable_from_hash(self, security_settings: SecuritySettings) -> None:
        hasher = PasswordHasherService(security_settings)
        assert "plaintext-password" not in hasher.hash("plaintext-password")

    def test_malformed_hash_returns_false(self, security_settings: SecuritySettings) -> None:
        """A corrupt stored hash must deny access, not raise a 500."""
        hasher = PasswordHasherService(security_settings)
        assert hasher.verify("not-a-real-argon2-hash", "anything") is False

    def test_needs_rehash_on_stronger_parameters(self, security_settings: SecuritySettings) -> None:
        """A hash made under weaker settings is flagged for transparent upgrade."""
        weak = PasswordHasherService(security_settings).hash("pw")
        stronger = PasswordHasherService(_security(argon2_time_cost=4))
        assert stronger.needs_rehash(weak) is True

    def test_needs_rehash_false_for_current(self, security_settings: SecuritySettings) -> None:
        hasher = PasswordHasherService(security_settings)
        assert hasher.needs_rehash(hasher.hash("pw")) is False


class TestTokenService:
    def test_roundtrip_access_token(self, auth_settings: AuthSettings) -> None:
        service = TokenService(auth_settings)
        payload = service.decode(service.create_access_token("user-1"))
        assert payload["sub"] == "user-1"
        assert payload["typ"] == "access"

    def test_extra_claims_preserved(self, auth_settings: AuthSettings) -> None:
        service = TokenService(auth_settings)
        token = service.create_access_token("u", username="alice", roles=["ADMIN"])
        payload = service.decode(token)
        assert payload["username"] == "alice"
        assert payload["roles"] == ["ADMIN"]

    def test_refresh_token_rejected_as_access_token(self, auth_settings: AuthSettings) -> None:
        """Type confusion would let a long-lived refresh token call the whole API."""
        service = TokenService(auth_settings)
        with pytest.raises(TokenError, match="Expected a access token"):
            service.decode(service.create_refresh_token("u"), expected_type="access")

    def test_tampered_signature_rejected(self, auth_settings: AuthSettings) -> None:
        service = TokenService(auth_settings)
        token = service.create_access_token("u")
        head, body, signature = token.split(".")
        forged = f"{head}.{body}.{'A' * len(signature)}"
        with pytest.raises(TokenError):
            service.decode(forged)

    def test_token_from_other_secret_rejected(self, auth_settings: AuthSettings) -> None:
        """Rotating the signing key must invalidate every existing token."""
        issued = TokenService(auth_settings).create_access_token("u")
        other = TokenService(_auth(jwt_secret_key="z" * 32))
        with pytest.raises(TokenError):
            other.decode(issued)

    def test_expired_token_rejected(self, auth_settings: AuthSettings) -> None:
        service = TokenService(_auth(access_token_ttl_seconds=-1))
        with pytest.raises(TokenError, match="expired"):
            service.decode(service.create_access_token("u"))

    def test_wrong_issuer_rejected(self, auth_settings: AuthSettings) -> None:
        """Guards against a token minted by a different system sharing the secret."""
        foreign = jwt.encode(
            {
                "sub": "u",
                "typ": "access",
                "iss": "somewhere-else",
                "iat": dt.datetime.now(dt.UTC),
                "exp": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            },
            auth_settings.jwt_secret_key.get_secret_value(),
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            TokenService(auth_settings).decode(foreign)

    def test_missing_required_claim_rejected(self, auth_settings: AuthSettings) -> None:
        incomplete = jwt.encode(
            {"sub": "u", "iss": auth_settings.jwt_issuer},  # no exp/iat/typ
            auth_settings.jwt_secret_key.get_secret_value(),
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            TokenService(auth_settings).decode(incomplete)

    def test_unsigned_token_rejected(self, auth_settings: AuthSettings) -> None:
        """The classic alg=none downgrade must not be accepted."""
        unsigned = jwt.encode(
            {"sub": "u", "typ": "access", "iss": auth_settings.jwt_issuer},
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenError):
            TokenService(auth_settings).decode(unsigned)

    def test_each_token_has_unique_jti(self, auth_settings: AuthSettings) -> None:
        """Phase 6 revocation keys on jti, so it must actually be unique."""
        service = TokenService(auth_settings)
        first = service.decode(service.create_access_token("u"))
        second = service.decode(service.create_access_token("u"))
        assert first["jti"] != second["jti"]


class TestSecretCipher:
    def test_roundtrip(self, security_settings: SecuritySettings) -> None:
        cipher = SecretCipher(security_settings)
        assert cipher.decrypt(cipher.encrypt("ldap-bind-password")) == "ldap-bind-password"

    def test_ciphertext_hides_plaintext(self, security_settings: SecuritySettings) -> None:
        cipher = SecretCipher(security_settings)
        assert "ldap-bind-password" not in cipher.encrypt("ldap-bind-password")

    def test_key_rotation_makes_old_ciphertext_undecryptable(
        self, security_settings: SecuritySettings
    ) -> None:
        """A database dump is useless without the key mounted outside the database."""
        ciphertext = SecretCipher(security_settings).encrypt("secret")
        other = SecretCipher(
            _security(encryption_key="cHJpdmF0ZS1haS1wbGF0Zm9ybS10ZXN0LWtleS0wMDI=")
        )
        with pytest.raises(ValueError, match="Could not decrypt"):
            other.decrypt(ciphertext)

    def test_invalid_key_fails_fast(self, security_settings: SecuritySettings) -> None:
        """A malformed key must fail at startup, not at first tool registration."""
        with pytest.raises(ValueError, match="not a valid Fernet key"):
            SecretCipher(_security(encryption_key="nope"))


class TestApiKeys:
    def test_generated_key_verifies(self) -> None:
        full_key, prefix, stored_hash = generate_api_key()
        assert verify_api_key(full_key, stored_hash) is True
        assert full_key.startswith(f"aip_{prefix}_")

    def test_stored_hash_does_not_contain_key(self) -> None:
        """Only prefix + hash are persisted; the key itself must be unrecoverable."""
        full_key, _, stored_hash = generate_api_key()
        assert full_key not in stored_hash
        assert len(stored_hash) == 64  # sha256 hex

    def test_wrong_key_rejected(self) -> None:
        _, _, stored_hash = generate_api_key()
        other_key, _, _ = generate_api_key()
        assert verify_api_key(other_key, stored_hash) is False

    def test_keys_are_unique(self) -> None:
        assert len({generate_api_key()[0] for _ in range(50)}) == 50

    def test_hash_is_deterministic(self) -> None:
        """Gateway auth looks keys up by hash, so it must be stable."""
        full_key, _, _ = generate_api_key()
        assert hash_api_key(full_key) == hash_api_key(full_key)
