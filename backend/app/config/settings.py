"""M02 — Configuration & Environment.

Every tunable in the platform resolves through this module. Nothing else may read
``os.environ`` directly, and nothing may hard-code a password, URL, port, GPU id,
model path or container name (spec §M02, Rule 5).

Precedence, highest wins::

    constructor argument  >  environment variable  >  .env  >  config.yaml  >  field default

``config.yaml`` carries non-secret defaults and is committed. Secrets arrive from
the environment or ``.env``, which is not. Required secrets have no default, so a
missing one fails at *startup* rather than at first use — a container that boots
and then 500s on the first login is much harder to diagnose than one that refuses
to start.

Nested settings use a double-underscore delimiter: ``DATABASE__HOST`` sets
``settings.database.host``.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Repository-relative default. Overridable with PLATFORM_CONFIG_FILE so an
# air-gapped operator can mount a site-specific config without rebuilding.
DEFAULT_CONFIG_FILE = Path(__file__).with_name("config.yaml")

Environment = Literal["development", "staging", "production"]
GpuProbeKind = Literal["nvidia_smi", "dcgm", "fake"]
#: Runtimes the platform can serve a model with (M07, §28).
#:
#: Split by who owns the process, because the two halves behave differently in ways an
#: operator feels immediately:
#:
#: * **Managed** (vllm, sglang, tgi, llamacpp, mock) — the platform starts a container on
#:   a node, reserves GPUs for it, health-checks it and can stop or restart it.
#: * **External** (ollama, external) — the process already exists and the platform only
#:   points at it. No GPU reservation, no lifecycle: the platform cannot stop it, restart
#:   it, or tell you why it died.
ModelRuntime = Literal["vllm", "sglang", "tgi", "llamacpp", "ollama", "external", "mock"]

#: Runtimes the platform points at rather than deploys. A deployment for one of these
#: creates no container and reserves no GPUs.
EXTERNAL_RUNTIMES: frozenset[str] = frozenset({"ollama", "external"})


def external_key_for(settings: Settings, runtime: str, endpoint: str | None) -> str | None:
    """The hosted-provider key, but only for the endpoint that issued it.

    Defined once and used by both the gateway (every inference call) and the deployment
    health probe, because a credential rule that exists in two places eventually holds in
    one of them.

    The runtime alone is too coarse a test. `external` means "a server the platform points
    at rather than starts", which describes a hosted provider on the Internet *and* a
    llama.cpp container on this Docker network — both are `external`, and only one issued
    the key. `ollama` is in `EXTERNAL_RUNTIMES` for the same reason and is likewise local.
    Matching the endpoint is what keeps an OpenRouter token from being posted to localhost.
    """
    if runtime != "external":
        return None
    configured = (settings.models.external_endpoint or "").rstrip("/")
    if not configured or (endpoint or "").rstrip("/") != configured:
        return None
    return settings.models.external_api_key.get_secret_value() or None


# ---------------------------------------------------------------------------
# YAML settings source
# ---------------------------------------------------------------------------
class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """Feeds ``config.yaml`` into the settings chain as the lowest-priority source."""

    def __init__(self, settings_cls: type[BaseSettings], yaml_file: Path) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if yaml_file.is_file():
            loaded = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise TypeError(
                    f"{yaml_file} must contain a YAML mapping at the top level, "
                    f"got {type(loaded).__name__}"
                )
            self._data = loaded

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


# ---------------------------------------------------------------------------
# Configuration categories (§M02)
# ---------------------------------------------------------------------------
class PlatformSettings(BaseModel):
    name: str = "Private AI Platform"
    environment: Environment = "development"
    debug: bool = False
    version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    # The OpenAI-compatible surface. Its own root, not a sub-path of api_prefix: the
    # stock SDK derives every path from base_url, so `GET {base}/models` would otherwise
    # collide with the platform's own model registry (§8, M09).
    openai_prefix: str = "/v1"
    # Public base URL handed to developers by the portal (M20). Never a container IP (§12).
    public_base_url: str = "http://localhost:8080"


class DatabaseSettings(BaseModel):
    host: str = "postgres"
    port: int = 5432
    name: str = "ai_platform"
    user: str = "ai_platform"
    password: SecretStr  # required — no default on purpose
    pool_size: int = 10
    max_overflow: int = 20
    pool_pre_ping: bool = True
    echo: bool = False
    connect_timeout_seconds: int = 10

    #: Databases the platform provisions on this server for components that keep their
    #: own schema — Open WebUI (M17) and Langfuse (M19).
    #:
    #: Both are created unconditionally, including on a site that never starts the
    #: `monitoring` profile. An empty database costs nothing, and the alternative is
    #: that enabling Langfuse later needs a config change and a reseed before it will
    #: start — discovered at the moment somebody wanted it working.
    #:
    #: Created by `make seed`, not by a `/docker-entrypoint-initdb.d` script: those run
    #: only when the data directory is first initialised, so an existing install would
    #: silently never get them and the failure would surface as a component that cannot
    #: start. Seeding is idempotent and runs on every install and upgrade.
    companion_databases: list[str] = Field(default_factory=lambda: ["open_webui", "langfuse"])

    def companion_dsn(self, name: str) -> str:
        """libpq DSN for a companion database, for components that use their own driver."""
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{name}"
        )

    @property
    def dsn(self) -> str:
        """SQLAlchemy async DSN."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def libpq_dsn(self) -> str:
        """libpq-style DSN for the ``psql`` and ``pg_dump`` command-line tools (M25).

        Not for SQLAlchemy — Alembic uses the async DSN above, so the platform
        bundles exactly one PostgreSQL driver.
        """
        return (
            f"postgresql://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class RedisSettings(BaseModel):
    host: str = "valkey"
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None
    socket_timeout_seconds: float = 5.0

    @property
    def dsn(self) -> str:
        auth = f":{self.password.get_secret_value()}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class QdrantSettings(BaseModel):
    host: str = "qdrant"
    port: int = 6333
    grpc_port: int = 6334
    api_key: SecretStr | None = None
    prefer_grpc: bool = False
    timeout_seconds: float = 10.0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class MinioSettings(BaseModel):
    endpoint: str = "minio:9000"
    access_key: str = "ai-platform"
    secret_key: SecretStr  # required
    secure: bool = False
    bucket: str = "ai-platform"
    region: str = "us-east-1"


class AuthSettings(BaseModel):
    jwt_secret_key: SecretStr  # required
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 604800  # 7 days
    jwt_issuer: str = "private-ai-platform"

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_email: str = "admin@ai-platform.local"
    # Blank/absent means "do not create a bootstrap admin".
    bootstrap_admin_password: SecretStr | None = None

    @field_validator("jwt_secret_key")
    @classmethod
    def _reject_short_secret(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError(
                "AUTH__JWT_SECRET_KEY must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )
        return v


class OidcSettings(BaseModel):
    """An OpenID Connect provider — Keycloak on the internal network, typically (M03).

    Disabled by default. An air-gapped site that has not deployed an IdP must not be
    presented with a sign-in button that cannot work.
    """

    enabled: bool = False
    display_name: str = "Single sign-on"
    #: Issuer URL. Discovery reads `{issuer}/.well-known/openid-configuration`, so the
    #: endpoints, and the signing keys, come from the provider rather than from config
    #: that drifts out of step with it after a key rotation.
    issuer: str = ""
    client_id: str = ""
    client_secret: SecretStr | None = None
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    #: Claim carrying group membership. Keycloak emits `groups`; some deployments use
    #: `roles` or a namespaced claim, so it is configurable rather than assumed.
    groups_claim: str = "groups"
    username_claim: str = "preferred_username"
    #: Seconds of clock skew tolerated when validating `exp`/`iat`. Air-gapped hosts drift
    #: further than internet-synced ones.
    leeway_seconds: int = 30
    verify_tls: bool = True


class LdapSettings(BaseModel):
    """Active Directory or another LDAP directory (M03).

    Authentication is a **bind as the person**, not a lookup-and-compare: the directory
    stays the only thing that ever sees the password, and the platform never needs a hash
    it would then have to protect.
    """

    enabled: bool = False
    display_name: str = "Directory account"
    server_uri: str = ""
    #: Service account used to search for the person's DN before binding as them, and to
    #: read their groups afterwards. Read-only in the directory: the platform never writes.
    bind_dn: str = ""
    bind_password: SecretStr | None = None
    user_search_base: str = ""
    #: `{username}` is substituted. sAMAccountName for AD, uid for OpenLDAP.
    user_filter: str = "(sAMAccountName={username})"
    group_search_base: str = ""
    group_filter: str = "(member={user_dn})"
    email_attribute: str = "mail"
    full_name_attribute: str = "displayName"
    start_tls: bool = True
    timeout_seconds: int = 10


class FederationSettings(BaseModel):
    """What an external provider's groups entitle someone to (M03).

    The whole point of keeping this in configuration: a directory group name is a claim
    from outside the platform, and letting one *be* a role would mean anyone who can create
    a group in AD can grant themselves platform privileges.
    """

    #: `directory group name -> platform role name`. Case-insensitive on the group.
    role_mapping: dict[str, str] = Field(default_factory=dict)
    #: Granted to every federated user who matched no mapping. Empty means a person can
    #: sign in and see nothing, which is the safe default for a directory the platform
    #: does not control.
    default_roles: list[str] = Field(default_factory=lambda: ["USER"])
    #: Re-apply the mapping on every sign-in. On by default, so removing someone from a
    #: group in AD actually removes their platform access; turning it off means the
    #: directory stops being authoritative and roles have to be managed here instead.
    sync_roles_on_login: bool = True
    #: Create a platform account the first time someone authenticates successfully. Off
    #: means an administrator must create the account first, which some sites require.
    auto_provision: bool = True


class SecuritySettings(BaseModel):
    # Fernet key for envelope-encrypting tool credentials at rest (Phase 4 uses it;
    # declared now so the key exists before the first secret does).
    encryption_key: SecretStr
    # argon2id parameters. Defaults follow OWASP guidance; raise on beefier hardware.
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 4
    # Trusted CIDRs allowed to assert an end-user identity via forwarded headers
    # (M17: Open WebUI attribution). Empty means the platform trusts nobody.
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)


class DockerSettings(BaseModel):
    socket: str = "unix:///var/run/docker.sock"
    network: str = "ai-platform"
    # Label stamped on every container the platform creates, so it can tell its
    # own workloads apart from anything else on the host.
    managed_label: str = "ai-platform.managed"
    api_timeout_seconds: int = 60


class GpuSettings(BaseModel):
    probe: GpuProbeKind = "fake"
    poll_interval_seconds: int = 15
    metric_retention_days: int = 30
    # FakeGpuProbe shape — lets the whole GPU surface be exercised without hardware.
    fake_device_count: int = 4
    fake_device_model: str = "NVIDIA A100-SXM4-80GB"
    fake_memory_total_mib: int = 81920


class EnrollmentSettings(BaseModel):
    """Node self-enrolment (M04).

    Defaults suit a private, segmented GPU subnet — which is the only place this platform
    is meant to run. Two of them are deliberately permissive and are called out in
    ``app/core/agent_url.py``: private address ranges are allowed, and a source-IP mismatch
    is recorded rather than refused.
    """

    # An hour: long enough to walk to a rack and paste into a terminal, short enough that
    # a token left in a ticket or shell history is dead before anyone finds it.
    token_ttl_seconds: int = Field(default=3600, ge=300, le=86400)

    #: Where `install.sh` stages the four files a node needs (M04). Mounted read-only, so
    #: the control plane can serve them and can reach nothing else on the host.
    #:
    #: A node needs ~288 MB — the installer, its helpers, the manifest it verifies against,
    #: and the node-agent image. The rest of a 1.9 GB bundle is postgres, qdrant, minio and
    #: the control plane's own images, which a node never runs. Serving this is not a Rule 4
    #: violation: the bytes arrived on the bundle already, and this moves them one hop
    #: inside the same isolated network rather than fetching them from the Internet.
    node_bundle_path: str = "/data/node-bundle"
    retention_days: int = 7
    # Bounds how many outbound probes one token can cause. Held in Postgres rather than
    # Redis so it survives a cache outage and cannot be evaded by rotating source IP.
    max_attempts_per_token: int = 5
    rate_limit_per_minute_per_ip: int = 10
    default_agent_port: int = 9100
    # The highest-leverage control in the whole flow: a node agent has no business on 22
    # or 5432, and pinning the port removes most of the value of an SSRF primitive.
    allowed_agent_ports: list[int] = Field(default_factory=lambda: [9100])
    # Empty means "any address that passes the base checks". A site that sets this to its
    # GPU subnet makes a stolen enrolment token useless outside the building.
    allowed_advertise_cidrs: list[str] = Field(default_factory=list)
    require_https: bool = False
    require_source_ip_match: bool = False
    allow_node_name_mismatch: bool = False
    # When true an enrolled node arrives labelled `disabled`, which the existing
    # `list_pollable` filter already honours — the node is visible but inert until an
    # administrator clears the label. A two-person control built from parts that exist.
    enroll_disabled: bool = False


class ModelsSettings(BaseModel):
    root_path: str = "/data/models"
    manifest_path: str = "/app/models/manifests"
    default_runtime: ModelRuntime = "mock"
    deployment_health_timeout_seconds: int = 900
    deployment_health_interval_seconds: int = 5

    # Container image per runtime. Configuration rather than code, because an
    # air-gapped site pins its own digests when building the offline bundle, and
    # because the vLLM image must match the host's CUDA driver — a mismatch is a
    # deployment-time failure the platform cannot detect in advance.
    #
    # Images must already be present on the node; the platform never pulls (Rule 4).
    runtime_images: dict[str, str] = Field(
        default_factory=lambda: {
            "mock": "ai-platform/mock-vllm:0.1.0",
            "vllm": "vllm/vllm-openai:v0.11.0",
            # Digests are pinned by the site when it builds its offline bundle; these
            # tags are the upstream names to pull on a connected build machine. Not
            # verified on GPU hardware here — see docs/models.md.
            "sglang": "lmsysorg/sglang:v0.4.1-cu124",
            "tgi": "ghcr.io/huggingface/text-generation-inference:3.0.1",
            "llamacpp": "ghcr.io/ggml-org/llama.cpp:server",
            # External runtimes have no image on purpose: the platform never starts them.
        }
    )

    #: Where an already-running Ollama lives. `host.docker.internal` reaches the Docker
    #: host from inside a container, which is what "I have Ollama on my machine" means in
    #: practice — Ollama binds to the host, not to the platform's network.
    #:
    #: On Linux this name needs `extra_hosts: host-gateway` in compose; on macOS and
    #: Windows Docker Desktop provides it.
    ollama_endpoint: str = "http://host.docker.internal:11434"

    #: An authenticated OpenAI-compatible endpoint somewhere else entirely — OpenRouter by
    #: default, but anything speaking the same protocol works, which is why these are not
    #: named after a vendor.
    #:
    #: **Enabling this makes the installation no longer air-gapped.** Every prompt leaves
    #: the host, including passages retrieved from knowledge bases. That is a
    #: classification decision rather than a technical one, so it is opt-in: with
    #: `external_api_key` empty the platform behaves exactly as before, and
    #: `make external-import` refuses rather than registering a model that cannot answer.
    #: The **root**, without `/v1`. The provider appends `/v1/chat/completions` itself, and
    #: the health path below appends `/v1/models` — exactly as for Ollama, whose endpoint
    #: is likewise a bare host and port. Including `/v1` here produces `/v1/v1/...` and a
    #: 404 that reads as "nothing is answering at the endpoint".
    external_endpoint: str = "https://openrouter.ai/api"
    #: `SecretStr` so the key cannot reach a log line, a traceback or `/config` output.
    external_api_key: SecretStr = SecretStr("")
    #: The model id as the provider names it, e.g. `vendor/model-name`.
    external_model: str = ""

    #: Health probe path per runtime. vLLM and the mock expose `/health`; Ollama does
    #: not, but every OpenAI-compatible server answers `/v1/models`, which is why that is
    #: the fallback rather than a special case.
    runtime_health_paths: dict[str, str] = Field(
        default_factory=lambda: {
            "mock": "/health",
            "vllm": "/health",
            "sglang": "/health",
            "tgi": "/health",
            "llamacpp": "/health",
            "ollama": "/v1/models",
            "external": "/v1/models",
        }
    )

    # Requests exceeding this are rejected by the gateway before reaching a runtime.
    max_prompt_tokens: int = 128_000


class GatewaySettings(BaseModel):
    """OpenAI-compatible surface (M09, M17)."""

    #: Header carrying a *signed* forwarded identity. Preferred over the plaintext ones
    #: below, and the only one accepted once a client has a signing secret configured.
    identity_jwt_header: str = "X-OpenWebUI-User-Jwt"
    #: Expected `iss` claim. Rejecting anything else keeps a token minted for some other
    #: purpose from being replayed here as an identity assertion.
    identity_jwt_issuer: str = "open-webui"
    #: Claims to read the end user from, in precedence order. Email leads because it is
    #: what a chargeback report has to show and what matches a platform account; `sub` is
    #: the fallback when a frontend has no email.
    identity_jwt_claims: list[str] = Field(default_factory=lambda: ["email", "sub"])

    #: Plaintext headers a trusted client may use instead, in precedence order. Defaults
    #: match Open WebUI's `ENABLE_FORWARD_USER_INFO_HEADERS`.
    #:
    #: Configuration rather than code because the frontend is replaceable — swapping
    #: Open WebUI for another chat UI should not require editing the gateway.
    #:
    #: Read only from a client with `trusted_identity_headers` set, and never from one
    #: that has a signing secret: falling back to plaintext there would hand an attacker
    #: a way to bypass the signature simply by omitting it.
    identity_headers: list[str] = Field(
        default_factory=lambda: [
            "X-OpenWebUI-User-Email",
            "X-OpenWebUI-User-Id",
        ]
    )

    #: The API client `chat-key` provisions for the platform's own chat frontend (M17).
    chat_client_name: str = "open-webui"
    #: One key carries every person's chat traffic, so this is a fleet limit rather than
    #: a per-user one — sized for a room full of people, not for one application.
    chat_rate_limit_per_minute: int = 6_000


class KnowledgeSettings(BaseModel):
    """Knowledge bases and memory (M15, M16)."""

    #: The scope a request belongs to when nothing says otherwise. A single-tenant install
    #: leaves this alone; it exists as a value rather than a NULL so every filter has
    #: something to match, and no code path needs a "no tenant" special case — which is
    #: where a leak would get in.
    default_tenant: str = "default"

    #: The model semantic memory is embedded with. Separate from a knowledge base's model,
    #: which is per base: memory is one shared collection, so it needs one fixed model, and
    #: changing it invalidates every stored vector.
    memory_embedding_model: str = "enterprise-embed"

    #: Ingestion defaults for a new knowledge base.
    default_chunk_size: int = 1200
    default_chunk_overlap: int = 150

    #: How long a derived memory lasts before it stops being recalled, in days. Stated
    #: memories ("call me Sam") are kept; inferences expire, so an agent's picture of
    #: someone does not ossify around something it guessed once.
    derived_memory_ttl_days: int = 90

    #: OCR for scanned pages and images (M28, Phase 9). An alias like every other model
    #: reference, so the engine behind it is swapped by repointing the alias rather than
    #: by editing configuration.
    #:
    #: A document needing OCR when nothing serves this alias lands in NO_TEXT with a
    #: reason — the pipeline does not fail, and the operator is told what to deploy.
    ocr_model: str = "enterprise-ocr"

    #: Scripts to recognise. Both by default: a document mixing an Arabic body with
    #: English identifiers is the normal case here, not an edge one.
    ocr_languages: list[str] = Field(default_factory=lambda: ["en", "ar"])


class McpSettings(BaseModel):
    #: Where MCP server manifests live, mounted into the container. Declarative
    #: registration, the same pattern as models: the manifests ship with the bundle beside
    #: the images `make mcp-vendor` built.
    manifest_path: str = "/app/mcp/manifests"
    health_interval_seconds: int = 60
    call_timeout_seconds: int = 30
    discovery_timeout_seconds: int = 15


class AgentsSettings(BaseModel):
    #: Declarative agent, skill and tool definitions, the same pattern as models and MCP
    #: servers: they ship inside the bundle, so an air-gapped install catalogues its
    #: agents by importing files rather than by an operator retyping prompts through a
    #: form — which is how two sites end up with agents that quietly differ.
    #:
    #: Three directories rather than one, because the import order matters: a skill
    #: names the tools it needs and an agent names both, so tools must exist first.
    tool_manifest_path: str = "/app/tools"
    skill_manifest_path: str = "/app/skills"
    agent_manifest_path: str = "/app/agents"

    max_iterations: int = 25
    run_timeout_seconds: int = 600
    default_temperature: float = 0.2
    # Risk levels that halt the run pending human approval (§10, §M24).
    approval_required_risk_levels: list[str] = Field(default_factory=lambda: ["HIGH", "CRITICAL"])
    # Tool types deliberately disabled: §M12 lists them but §25 forbids
    # unrestricted shell execution. They stay off until a hardened executor
    # exists (see docs/security.md).
    disabled_tool_types: list[str] = Field(default_factory=lambda: ["COMMAND", "PYTHON"])


class LoggingSettings(BaseModel):
    # populate_by_name lets the field be set as either `json` (its config-file and
    # env-var name) or `json_logs` (its Python name).
    model_config = ConfigDict(populate_by_name=True)

    level: str = "INFO"
    # Named `json_logs` internally because a field literally called `json` shadows
    # BaseModel.json and pydantic warns about it. The alias keeps the external name
    # as LOGGING__JSON / logging.json, which is what an operator expects to set.
    json_logs: bool = Field(default=True, alias="json")
    # Header names that must never reach the logs.
    redact_headers: list[str] = Field(
        default_factory=lambda: ["authorization", "cookie", "x-api-key", "proxy-authorization"]
    )

    @field_validator("level")
    @classmethod
    def _validate_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOGGING__LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return upper


class AirgapSettings(BaseModel):
    enforced: bool = True
    bundle_path: str = "/opt/ai-platform/offline"
    # Hosts the platform may legitimately reach. Anything else is a bug (Rule 4).
    allowed_egress_hosts: list[str] = Field(default_factory=list)


class WorkersSettings(BaseModel):
    """Background job control (M22).

    Disabled in tests and anywhere a second replica would double every scheduled job —
    the Phase 1 workers have no distributed lock, so exactly one instance must run them
    (see `app/workers/scheduler.py`).
    """

    enabled: bool = True
    # Node polling cadence is `gpu.poll_interval_seconds`; these are the jobs that do
    # not track it.
    container_sync_interval_seconds: int = 60


class TracingSettings(BaseModel):
    """Distributed tracing to Tempo over OTLP (M19, Phase 7).

    Off by default, and that is not timidity: an exporter pointed at a collector that
    is not deployed queues spans, retries, and logs an export failure every few seconds
    for the life of the process. A site that has not started the monitoring profile
    should see nothing at all.

    Tracing being off changes no behaviour elsewhere. The request id in
    :mod:`app.core.request_context` is what correlates logs, and it exists either way.
    """

    enabled: bool = False
    #: Collector base URL. The exporter appends `/v1/traces`, matching the OTLP/HTTP
    #: spec, so this is the collector root rather than the signal path.
    endpoint: str = "http://tempo:4318"
    #: `service.name` on every span. Distinguishes replicas of the control plane from
    #: the node agents when both report to one Tempo.
    service_name: str = "ai-platform-backend"
    #: Fraction of traces recorded, 0.0 to 1.0. A GPU host under load produces far more
    #: spans than an air-gapped Tempo with local disk wants to keep.
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    export_timeout_seconds: float = 10.0


class LangfuseSettings(BaseModel):
    """LLM observability for agent runs (M19, Phase 7).

    Off by default, like tracing, and for the same reason.

    The platform talks to Langfuse's ingestion API directly rather than through the
    `langfuse` SDK. The SDK would add six packages to the air-gapped bundle including
    `requests` and `urllib3` — a second HTTP stack beside httpx — which is the same
    objection that kept LangGraph out in Phase 4. What is sent is a documented JSON
    batch; see `app/services/langfuse.py`.
    """

    enabled: bool = False
    host: str = "http://langfuse:3000"
    public_key: str = ""
    secret_key: SecretStr | None = None
    #: Events are batched and flushed by the background worker. A run that ends is
    #: never held up waiting for an observability system to acknowledge it.
    flush_interval_seconds: int = 10
    flush_batch_size: int = 50
    timeout_seconds: float = 10.0


class VoiceSettings(BaseModel):
    """The speech-to-speech assistant (M29).

    Every default here is deliberately conservative, because voice is the most sensitive
    input this platform takes: a microphone in an office records whoever is nearby, not
    only the person who pressed the button.

    **Models are named by alias, never by endpoint.** `enterprise-transcribe` resolves
    through the registry to whatever a site actually deployed — a local faster-whisper,
    or the synthetic engine on a laptop. Putting a URL here would create a second way to
    reach a model that the registry does not know about, which is how one component ends
    up talking to a runtime the rest of the platform thinks is gone.
    """

    enabled: bool = False
    #: `auto` asks the engine to detect. Forcing a language does not fail on the wrong
    #: one — it returns fluent nonsense — so detection is the default (M26).
    default_language: Literal["auto", "en", "ar"] = "auto"
    stt_model: str = "enterprise-transcribe"
    tts_model: str = "enterprise-speak"
    default_voice: str = ""
    #: The agent a session talks to when the caller names none.
    default_agent_slug: str = ""

    #: PCM 16-bit mono at 16 kHz — what every ASR engine resamples to anyway, so the
    #: browser sends what the engine wants and nothing transcodes in between.
    sample_rate_hz: int = 16000

    #: A session holds a WebSocket, an agent and possibly a GPU. Bounded so a forgotten
    #: browser tab cannot hold them for a working day.
    max_session_seconds: int = 900
    idle_timeout_seconds: int = 120

    #: Barge-in. On by default: an assistant that cannot be interrupted is one people
    #: stop using, because the only way out of a wrong answer is to wait for it.
    interrupt_enabled: bool = True
    vad_enabled: bool = True

    #: **Off by default, and the most important defaults in this class.** Raw audio is
    #: biometric data; a transcript is what somebody said. Neither is retained unless a
    #: site decides it needs to and says so (§29). When enabled, audio goes to MinIO —
    #: never into PostgreSQL, which has no lifecycle for objects that size.
    store_audio: bool = False
    store_transcripts: bool = True
    #: Days. Applies to whatever the two flags above allow to be stored.
    retention_days: int = 30


class HealthSettings(BaseModel):
    """Which dependencies must be up for /health/ready to return 200.

    Kept configurable because readiness requirements change per phase: Qdrant and
    MinIO only start mattering at Phase 5, and a deployment that does not use RAG
    should not be held down by them.
    """

    required: list[str] = Field(default_factory=lambda: ["postgres", "redis", "qdrant", "minio"])
    probe_timeout_seconds: float = 5.0


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """Root configuration object. Obtain it via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
        # Surface every problem at once instead of one per restart.
        validate_default=True,
    )

    platform: PlatformSettings = Field(default_factory=PlatformSettings)
    database: DatabaseSettings
    redis: RedisSettings = Field(default_factory=RedisSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    minio: MinioSettings
    auth: AuthSettings
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    ldap: LdapSettings = Field(default_factory=LdapSettings)
    federation: FederationSettings = Field(default_factory=FederationSettings)
    security: SecuritySettings
    docker: DockerSettings = Field(default_factory=DockerSettings)
    gpu: GpuSettings = Field(default_factory=GpuSettings)
    enrollment: EnrollmentSettings = Field(default_factory=EnrollmentSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    airgap: AirgapSettings = Field(default_factory=AirgapSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    workers: WorkersSettings = Field(default_factory=WorkersSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first."""
        import os

        config_file = Path(os.environ.get("PLATFORM_CONFIG_FILE", DEFAULT_CONFIG_FILE))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            YamlConfigSettingsSource(settings_cls, config_file),
        )

    @property
    def is_production(self) -> bool:
        return self.platform.environment == "production"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so configuration is read and validated exactly once per process.
    Tests that need a different configuration call ``get_settings.cache_clear()``.
    """
    # Fields without defaults (passwords, keys) are supplied by the settings
    # sources, not by the caller.
    return Settings()
