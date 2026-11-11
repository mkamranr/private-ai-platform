"""Support-bundle redaction (E7): structure kept, secrets gone."""

from __future__ import annotations

from pydantic import SecretStr

from app.core.redact import REDACTED, is_secret_key, redact_text, redact_value


class TestKeys:
    def test_secret_names_match_by_substring(self):
        for name in (
            "password",
            "DB_PASSWORD",
            "x_api_token",
            "jwt_secret_key",
            "Authorization",
            "minio_secret_key",
            "agent-token",
            "cookie",
            "database_dsn",
            "signing_key",
        ):
            assert is_secret_key(name), name

    def test_lookalikes_are_not_secret(self):
        for name in (
            "max_tokens",
            "total_tokens",
            "key_fingerprint",
            "redact_headers",
            "username",
            "host",
            "token_ttl_minutes",
        ):
            assert not is_secret_key(name), name


class TestValues:
    def test_nested_structures_keep_their_shape(self):
        out = redact_value(
            {
                "database": {"host": "postgres", "password": SecretStr("pw-1")},
                "nodes": [{"name": "gpu-1", "agent_token": "tok-123"}],
                "max_tokens": 512,
                "empty_secret": None,
            }
        )
        assert out == {
            "database": {"host": "postgres", "password": REDACTED},
            "nodes": [{"name": "gpu-1", "agent_token": REDACTED}],
            "max_tokens": 512,
            "empty_secret": None,
        }

    def test_secret_types_are_redacted_whatever_the_key(self):
        assert redact_value({"anything": SecretStr("x")}) == {"anything": REDACTED}

    def test_strings_inside_structures_are_scrubbed(self):
        out = redact_value({"error": "connect postgresql://app:s3cret@db:5432/x failed"})
        assert "s3cret" not in out["error"] and "db:5432" in out["error"]


class TestText:
    def test_url_credentials(self):
        assert redact_text("redis://:hunter2@valkey:6379/0") == f"redis://:{REDACTED}@valkey:6379/0"
        assert "user" in redact_text("https://user:pw@host/")

    def test_authorization_values(self):
        line = "GET /v1/models Authorization: Bearer abcdefghijklmnop1234"
        assert "abcdefghijklmnop1234" not in redact_text(line)
        assert "Basic" in redact_text("Basic dXNlcjpwYXNzd29yZA==")
        assert "dXNlcjpwYXNzd29yZA" not in redact_text("Basic dXNlcjpwYXNzd29yZA==")

    def test_jwts_and_api_keys(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        assert jwt not in redact_text(f"token was {jwt}")
        key = "aip_Zx81kQ0w2m3n4b5v6c7x8z9a"
        assert key not in redact_text(f"called with {key} ok")

    def test_assignments(self):
        out = redact_text("DB_PASSWORD=pw1 OPS__RUNNER_TOKEN='abc' user=admin max_tokens=10")
        assert "pw1" not in out and "abc" not in out
        assert "user=admin" in out and "max_tokens=10" in out

    def test_private_keys(self):
        pem = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIBx\n-----END EC PRIVATE KEY-----"
        out = redact_text(f"key:\n{pem}\n")
        assert "MHcCAQEEIBx" not in out and "BEGIN EC PRIVATE KEY" in out

    def test_plain_text_is_untouched(self):
        line = "deployment llama-3 READY on gpu-1 after 42s"
        assert redact_text(line) == line
