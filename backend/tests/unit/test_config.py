"""Configuration layering and validation (M02)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import Settings, YamlConfigSettingsSource

_REQUIRED = {
    "DATABASE__PASSWORD": "pw",
    "MINIO__SECRET_KEY": "miniopassword",
    "AUTH__JWT_SECRET_KEY": "x" * 32,
    "SECURITY__ENCRYPTION_KEY": "k" * 44,
}


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set the minimum required secrets plus any overrides."""
    for key, value in {**_REQUIRED, **overrides}.items():
        monkeypatch.setenv(key, value)


# Every settings category. Used to scrub the ambient environment.
_CATEGORY_PREFIXES = (
    "PLATFORM__",
    "DATABASE__",
    "REDIS__",
    "QDRANT__",
    "MINIO__",
    "AUTH__",
    "SECURITY__",
    "DOCKER__",
    "GPU__",
    "MODELS__",
    "MCP__",
    "AGENTS__",
    "LOGGING__",
    "AIRGAP__",
    "HEALTH__",
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_body: str = "") -> None:
    """Make a settings test hermetic.

    Three sources have to be neutralised, not one:

    1. ``config.yaml`` — redirected to a throwaway file.
    2. ``.env`` — sidestepped by chdir, since Settings resolves it relative to cwd.
    3. **The process environment** — the real blocker. Compose injects the whole
       ``.env`` as actual environment variables, so DATABASE__PORT is already set
       inside the container at the *highest* priority. A precedence test asserting
       "yaml supplies the port" would fail against it, and correctly so.

    Scrubbing all three makes these tests about precedence rather than about
    whatever the container happens to have been started with.
    """
    for name in list(os.environ):
        if name.startswith(_CATEGORY_PREFIXES):
            monkeypatch.delenv(name, raising=False)

    config = tmp_path / "config.yaml"
    config.write_text(yaml_body)
    monkeypatch.setenv("PLATFORM_CONFIG_FILE", str(config))
    monkeypatch.chdir(tmp_path)


class TestPrecedence:
    """yaml < .env < environment variable."""

    def test_env_var_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolate(monkeypatch, tmp_path, "database:\n  host: from-yaml\n  port: 1111\n")
        _env(monkeypatch, DATABASE__HOST="from-env")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.database.host == "from-env"
        # The key the env var did not set still comes from yaml, proving the two
        # sources merge rather than one replacing the other wholesale.
        assert settings.database.port == 1111

    def test_yaml_applies_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path, "platform:\n  name: From YAML\n")
        monkeypatch.delenv("PLATFORM__NAME", raising=False)
        _env(monkeypatch)

        assert Settings().platform.name == "From YAML"  # type: ignore[call-arg]

    def test_field_default_applies_when_both_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch)

        assert Settings().database.pool_size == 10  # type: ignore[call-arg]


class TestRequiredSecrets:
    """A missing secret must fail at startup, not at first use."""

    @pytest.mark.parametrize("missing", sorted(_REQUIRED))
    def test_missing_required_secret_fails_construction(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing: str
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch)
        monkeypatch.delenv(missing)

        with pytest.raises(ValidationError):
            Settings()  # type: ignore[call-arg]

    def test_short_jwt_secret_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 32-character minimum is enforced, not merely documented."""
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, AUTH__JWT_SECRET_KEY="too-short")

        with pytest.raises(ValidationError, match="at least 32 characters"):
            Settings()  # type: ignore[call-arg]


class TestValidation:
    def test_invalid_log_level_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, LOGGING__LEVEL="CHATTY")

        with pytest.raises(ValidationError, match="LOGGING__LEVEL"):
            Settings()  # type: ignore[call-arg]

    def test_log_level_normalised_to_upper(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, LOGGING__LEVEL="debug")

        assert Settings().logging.level == "DEBUG"  # type: ignore[call-arg]

    def test_logging_json_alias(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """LOGGING__JSON maps to the internally renamed ``json_logs`` field."""
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, LOGGING__JSON="false")

        assert Settings().logging.json_logs is False  # type: ignore[call-arg]


class TestSecretsNeverLeak:
    def test_secrets_masked_in_dump(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`cli config` prints the effective configuration; it must not print secrets."""
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, DATABASE__PASSWORD="super-secret-value")

        dumped = Settings().model_dump_json()  # type: ignore[call-arg]

        assert "super-secret-value" not in dumped
        assert "**********" in dumped

    def test_dsn_exposes_password_only_on_demand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The DSN must contain the real password — it is handed to the driver."""
        _isolate(monkeypatch, tmp_path)
        _env(monkeypatch, DATABASE__PASSWORD="pw123")

        settings = Settings()  # type: ignore[call-arg]

        assert "pw123" in settings.database.dsn
        assert settings.database.dsn.startswith("postgresql+asyncpg://")
        assert settings.database.libpq_dsn.startswith("postgresql://")


class TestYamlSource:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """An absent config.yaml is valid — env vars alone may configure the platform."""
        source = YamlConfigSettingsSource(Settings, tmp_path / "nope.yaml")
        assert source() == {}

    def test_non_mapping_yaml_rejected(self, tmp_path: Path) -> None:
        """A list at the top level is a mistake worth failing loudly on."""
        bad = tmp_path / "bad.yaml"
        bad.write_text("- one\n- two\n")
        with pytest.raises(TypeError, match="mapping"):
            YamlConfigSettingsSource(Settings, bad)


class TestPlatformEmail:
    """Email validation must work on a closed network.

    ``pydantic.EmailStr`` rejects ``.local`` as a reserved domain and, with
    deliverability checking on, performs a DNS lookup. Both are wrong here: §25
    requires the platform to work with DNS unavailable, and the default bootstrap
    admin address in ``.env.example`` is ``admin@ai-platform.local``. Without this,
    an operator could not create a user with their own site's address.
    """

    @pytest.mark.parametrize(
        "address",
        [
            "admin@ai-platform.local",
            "ops@corp.internal",
            "svc@node01.lan",
            "real.person@example.com",
            "first+tag@sub.domain.example.org",
        ],
    )
    def test_internal_and_public_addresses_accepted(self, address: str) -> None:
        from app.core.validators import validate_platform_email

        assert validate_platform_email(address) == address

    def test_bare_hostname_domain_still_rejected(self) -> None:
        """A dotless domain (``user@localhost``) remains invalid.

        Unreserving ``localhost`` permits ``foo.localhost``, not a bare hostname —
        that rule is separate and worth keeping, since a dotless domain is far more
        often a typo than an intended address.
        """
        from app.core.validators import validate_platform_email

        with pytest.raises(ValueError, match="period"):
            validate_platform_email("user@localhost")

    @pytest.mark.parametrize(
        "address",
        ["no-at-sign", "two@@ats.com", "trailing@", "@leading.com", "spaces in@x.com"],
    )
    def test_malformed_addresses_still_rejected(self, address: str) -> None:
        """Relaxing reserved domains must not relax syntax checking."""
        from app.core.validators import validate_platform_email

        with pytest.raises(ValueError):
            validate_platform_email(address)

    @pytest.mark.parametrize("address", ["a@b.invalid", "a@b.test", "a@b.onion"])
    def test_genuinely_unusable_domains_still_rejected(self, address: str) -> None:
        """Only `local`/`localhost` are unreserved — these are never a real mailbox."""
        from app.core.validators import validate_platform_email

        with pytest.raises(ValueError):
            validate_platform_email(address)

    def test_address_is_normalised(self) -> None:
        """So Admin@Example.COM and admin@example.com cannot become two accounts."""
        from app.core.validators import validate_platform_email

        assert validate_platform_email("Admin@Example.COM") == "Admin@example.com"

    def test_bootstrap_admin_default_is_valid(self) -> None:
        """The shipped default must actually be creatable through the API."""
        from app.core.validators import validate_platform_email

        assert validate_platform_email("admin@ai-platform.local")
