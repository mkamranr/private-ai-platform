"""Runtime settings (expansion E8, plan §6 "Settings").

Two kinds of setting reach the Settings page, and the page must not blur them:

* **Runtime settings** — a short, curated list an administrator may change from the console
  without a restart. Each one is listed here only because something reads it per request
  or per worker run. A field that is only read at startup would accept an edit and keep
  doing what it did before, and a settings page that silently ignores what it saved is
  worse than one that says "restart required". Overrides live in ``system_settings`` under
  ``runtime.<group>.<field>``, the table that already existed for operational settings.
* **Deployment settings** — everything else (secrets, hosts, identity, token lifetimes,
  air-gap policy). Shown read-only, redacted, with the environment variable that sets it.
  Those stay out of the database on purpose: a value that can be edited through the UI is
  a value an attacker holding the UI can edit (§25, see ``app.models.system``).

Overrides are validated against the same pydantic model that validates the environment,
so an override can never hold a value the environment would have refused. A stored value
that no longer validates (an upgrade tightened a bound) is ignored and reported, not
applied.

**Propagation.** The effective settings are cached per process and reloaded at most every
``CACHE_SECONDS``. A change is invalidated immediately in the process that made it, and
reaches other processes (and the workers) within that window.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.redact import REDACTED, is_secret_key, redact_value
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.system import SystemSetting
from app.services.audit import AuditService

log = get_logger(__name__)

KEY_PREFIX = "runtime."
#: How stale another process's view of an override may be.
CACHE_SECONDS = 10.0

CATEGORIES: tuple[tuple[str, str], ...] = (
    ("general", "General"),
    ("security", "Security"),
    ("retention", "Retention"),
    ("networking", "Networking"),
    ("gateway", "Gateway resilience"),
    ("operations", "Operations"),
    ("monitoring", "GPU monitoring"),
    ("integrations", "Integrations"),
    ("chargeback", "Chargeback"),
    ("branding", "Email branding"),
)


def _http_base_url(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("must be a URL")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an absolute http(s) URL, for example https://ai.example.local")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("must not carry credentials, a query or a fragment")


def _one_of(*choices: str) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if value not in choices:
            raise ValueError("must be one of: " + ", ".join(choices))

    return check


def _bounded(low: int, high: int) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}")

    return check


@dataclass(frozen=True, slots=True)
class RuntimeSetting:
    """A setting the console may override. ``key`` is ``<group>.<field>`` of ``Settings``."""

    key: str
    category: str
    label: str
    description: str
    #: When an edit takes effect, in words the page shows next to the field.
    applies: str
    #: Extra checks beyond the pydantic field's own bounds.
    check: Callable[[Any], None] | None = None

    @property
    def group(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.key.split(".", 1)[1]

    @property
    def env_var(self) -> str:
        return f"{self.group}__{self.field}".upper()


#: Each entry's consumer is named in its description's last sentence, so a future edit that
#: stops reading one per request has an obvious place to notice.
RUNTIME_SETTINGS: tuple[RuntimeSetting, ...] = (
    RuntimeSetting(
        "platform.public_base_url",
        "networking",
        "Public base URL",
        "The address people and nodes use to reach this platform. Used in node install "
        "commands, workflow webhook URLs and the single sign-on callback.",
        "Next node enrolment, webhook rotation or sign-in",
        _http_base_url,
    ),
    RuntimeSetting(
        "gpu.metric_retention_days",
        "retention",
        "GPU metric retention (days)",
        "GPU samples older than this are deleted by the hourly retention job.",
        "Next retention run (hourly)",
        _bounded(1, 3650),
    ),
    RuntimeSetting(
        "knowledge.query_history_days",
        "retention",
        "Retrieval question history (days)",
        "How long a question asked in the Retrieval Playground is kept, with its passages, "
        "answer and faithfulness verdict, so it can be read again and replayed. 0 keeps none "
        "and deletes what is kept at the next retention run.",
        "Next question asked; pruning at the next retention run (hourly)",
        _bounded(0, 3650),
    ),
    RuntimeSetting(
        "agents.kill_switch",
        "security",
        "Stop all agents",
        "While on, no agent run starts (console, API, voice or agent: models through /v1), "
        "no agent approval is accepted, and running agents stop before their next model or "
        "tool call. Workflows are not affected. Read on every agent run and step.",
        "Within 10 seconds",
    ),
    RuntimeSetting(
        "gpu.alerts_enabled",
        "monitoring",
        "GPU threshold alerts",
        "Raise an alert when a GPU runs hot, nearly fills its memory or reports uncorrectable "
        "ECC errors. Turning this off clears the open GPU alerts. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_temperature_warning_celsius",
        "monitoring",
        "GPU warning temperature (°C)",
        "A GPU at or above this temperature raises a WARNING alert. Must be below the "
        "critical temperature. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_temperature_critical_celsius",
        "monitoring",
        "GPU critical temperature (°C)",
        "A GPU at or above this temperature raises a CRITICAL alert. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_memory_warning_percent",
        "monitoring",
        "GPU memory warning (%)",
        "A GPU with at least this share of its memory in use raises a WARNING alert. Model "
        "servers usually reserve 90-95% up front, so keep this above that. Read by the "
        "gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_sustain_minutes",
        "monitoring",
        "GPU alert: sustained for (minutes)",
        "A temperature or memory reading must stay over its level this many minutes before "
        "an alert is raised, so a short spike stays quiet. 0 raises on the first reading. "
        "Uncorrectable ECC errors always raise at once. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_recovery_margin_celsius",
        "monitoring",
        "GPU alert: recovery margin (°C)",
        "An open temperature alert clears only once the GPU is this many degrees under the "
        "level that raised it, so a GPU hovering at the line doesn't raise and clear it over "
        "and over. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gpu.alert_recovery_margin_percent",
        "monitoring",
        "GPU alert: recovery margin (memory %)",
        "An open memory alert clears only once usage is this many points under the warning "
        "level. Read by the gpu_alerts job.",
        "Next GPU alert check (every minute)",
    ),
    RuntimeSetting(
        "gateway.circuit_failure_threshold",
        "gateway",
        "Circuit breaker: failures to open",
        "Upstream failures within the window that take a deployment out of rotation.",
        "Next gateway request",
    ),
    RuntimeSetting(
        "gateway.circuit_window_seconds",
        "gateway",
        "Circuit breaker: window (seconds)",
        "How far back failures are counted.",
        "Next gateway request",
    ),
    RuntimeSetting(
        "gateway.circuit_open_seconds",
        "gateway",
        "Circuit breaker: open for (seconds)",
        "How long a tripped deployment is skipped before one trial request is let through.",
        "Next gateway request",
    ),
    RuntimeSetting(
        "gateway.load_balancing",
        "gateway",
        "Load balancing across copies",
        "How a call picks among a model's running copies: least_busy (fewest calls in "
        "flight), round_robin, or first (always the oldest; the others only on failure).",
        "Next gateway request",
        _one_of("least_busy", "round_robin", "first"),
    ),
    RuntimeSetting(
        "gateway.replica_retries",
        "gateway",
        "Retries on another copy",
        "How many other copies of the same model a failed call is tried on before the "
        "alias's fallback. A stream that already sent text is resumed on another copy "
        "only if the model can resume (see gateway.stream_resume_max_chars).",
        "Next gateway request",
        _bounded(0, 5),
    ),
    RuntimeSetting(
        "gateway.stream_resume_max_chars",
        "gateway",
        "Resume broken streams up to (characters)",
        "A stream that breaks after sending text is finished by another copy of the model, "
        "given the text sent so far to continue. Answers longer than this are not resumed; "
        "0 turns resuming off.",
        "Next gateway request",
        _bounded(0, 1_000_000),
    ),
    RuntimeSetting(
        "gateway.concurrency_lease_seconds",
        "gateway",
        "Concurrency lease (seconds)",
        "How long a concurrency slot is held for a request that never reports finishing.",
        "Next gateway request",
    ),
    RuntimeSetting(
        "ops.runner_stale_seconds",
        "operations",
        "Ops runner offline after (seconds)",
        "Without a heartbeat for this long, the host runner is shown offline and its "
        "running job may be abandoned.",
        "Immediately",
    ),
    RuntimeSetting(
        "ops.max_log_lines",
        "operations",
        "Output lines kept per operation",
        "Older lines of a backup, restore or upgrade log are dropped beyond this.",
        "Next runner report",
    ),
    RuntimeSetting(
        "ops.support_log_hours",
        "operations",
        "Support bundle: hours of logs",
        "How far back a support bundle collects platform logs from Loki.",
        "Next support bundle",
    ),
    RuntimeSetting(
        "ops.support_log_lines",
        "operations",
        "Support bundle: maximum log lines",
        "Upper bound on log lines in one support bundle.",
        "Next support bundle",
    ),
    RuntimeSetting(
        "agents.approval_ttl_hours",
        "security",
        "Approvals expire after (hours)",
        "An agent tool call or workflow step waiting for approval longer than this "
        "expires, and its run fails with a message saying so. Expiry is audited as the "
        "scheduler, never as a rejection. 0 waits forever. Read by the approval expiry job.",
        "Next expiry check (within 15 minutes)",
    ),
    RuntimeSetting(
        "agents.approval_reminder_hours",
        "security",
        "Remind before approvals expire (hours)",
        "An approval this close to expiring raises a reminder alert (source "
        '"approvals"), shown on the Alerts page and sent to notification channels '
        "subscribed to it. Cleared once someone decides. 0 turns reminders off; there "
        "are none while approvals never expire.",
        "Next expiry check (within 15 minutes)",
    ),
    RuntimeSetting(
        "agents.approval_reminder_waiting_hours",
        "security",
        "Remind about approvals that never expire (hours)",
        "Only while approvals never expire (expiry 0): an approval that has waited this "
        'long raises a reminder alert (source "approvals"), and an email to its '
        "approvers when that is on. Cleared once someone decides. 0 turns these off.",
        "Next expiry check (within 15 minutes)",
    ),
    RuntimeSetting(
        "agents.approval_reminder_email_approvers",
        "security",
        "Email reminders to each approver",
        "When a reminder is raised, also email each person who can decide that approval: "
        "active accounts holding tool.approve or workflow.approve that can see the agent's "
        "or workflow's team (at most 25 per approval), once each. Needs the mail server "
        "(NOTIFICATIONS__SMTP_HOST).",
        "Next expiry check (within 15 minutes)",
    ),
    RuntimeSetting(
        "notifications.email_brand_name",
        "branding",
        "Email brand name",
        "The name at the top of every notification and approver reminder email.",
        "Next email sent",
    ),
    RuntimeSetting(
        "notifications.email_brand_color",
        "branding",
        "Email accent colour",
        "A colour such as #4f46e5, used for the header mark and the button. Text on it "
        "turns dark or light to stay readable.",
        "Next email sent",
    ),
    RuntimeSetting(
        "notifications.email_brand_tagline",
        "branding",
        "Email tagline",
        "A short line under the brand name, for example the team that runs the platform. "
        "Empty for none.",
        "Next email sent",
    ),
    RuntimeSetting(
        "notifications.email_footer",
        "branding",
        "Email footer",
        "Text at the foot of every email, for example who to contact about an alert. "
        "Empty for none.",
        "Next email sent",
    ),
    RuntimeSetting(
        "notifications.email_subject_prefix",
        "branding",
        "Email subject prefix",
        "Put before every subject. Mail filters often match on it, so change it with care. "
        "Empty for none.",
        "Next email sent",
    ),
    RuntimeSetting(
        "ops.backup_interval_hours",
        "operations",
        "Scheduled backup every (hours)",
        "The platform queues a backup this often for the host runner. 0 turns scheduled "
        "backups off. Read by the backup schedule job.",
        "Next schedule check (within 5 minutes)",
    ),
    RuntimeSetting(
        "ops.backup_keep",
        "operations",
        "Backups to keep",
        "After a scheduled backup, older complete backups beyond this many are deleted. "
        "0 keeps every backup; the newest complete one is never deleted. Read by the "
        "backup schedule job.",
        "Next scheduled backup",
    ),
    RuntimeSetting(
        "ops.backup_keep_days",
        "operations",
        "Keep backups for (days)",
        "Complete backups older than this are deleted. 0 means no age limit. With "
        '"Backups to keep" also set, a backup is deleted only when it is both older '
        "than this and beyond that count. The newest complete backup is never deleted. "
        "Off-host copies are pruned the same way.",
        "Next scheduled backup",
    ),
    RuntimeSetting(
        "gateway.budget_warning_percent",
        "gateway",
        "Budget warning at (%)",
        "A daily or monthly token budget raises a warning alert at this share of its "
        "limit, and a critical one when it is used up. Read by the budget check job.",
        "Next budget check",
    ),
    RuntimeSetting(
        "gateway.enforce_platform_budgets",
        "gateway",
        "Enforce budgets on the platform's own calls",
        "When on, agent runs, workflow model steps, document ingestion, knowledge search "
        "and memory are refused once their team's or model's token budget is used up, "
        "as /v1 calls are. When off, they are counted against budgets but never refused. "
        "Read on every such call.",
        "Immediately",
    ),
    RuntimeSetting(
        "chargeback.currency",
        "chargeback",
        "Currency",
        "Three-letter code (ISO 4217) that cost rates and chargeback reports are shown "
        "in. A label only: changing it converts nothing. Read by every cost report.",
        "Next cost report",
    ),
    RuntimeSetting(
        "auth.mfa_required",
        "security",
        "Two-factor authentication required for",
        "off: anyone may turn it on for themselves. admins: superusers and the ADMIN "
        "and SUPER_ADMIN roles must use it. all: every local account must. Someone "
        "required but not set up does it at their next sign-in. Directory and single "
        "sign-on accounts are left to their provider. Read at every sign-in.",
        "Next sign-in",
        _one_of("off", "admins", "all"),
    ),
    RuntimeSetting(
        "auth.mfa_remember_days",
        "security",
        "Trust a browser for (days)",
        'After the two-factor step, a person may tick "trust this browser" and skip the '
        "step there for this many days. Changing the password, signing out everywhere or "
        "a 2FA reset forgets every trusted browser. 0 turns the option off. Read at every "
        "sign-in.",
        "Next sign-in",
    ),
)
BY_KEY = {s.key: s for s in RUNTIME_SETTINGS}


@dataclass(frozen=True, slots=True)
class DeploymentSetting:
    """Read-only: set in ``.env`` / ``config.yaml``, applied on restart."""

    key: str
    category: str
    label: str

    @property
    def env_var(self) -> str:
        group, field = self.key.split(".", 1)
        return f"{group}__{field}".upper()


#: Curated, not exhaustive: the Diagnostics page shows the whole redacted configuration.
#: Every entry is read somewhere in the code; a field nothing reads does not belong on a page
#: that implies changing it would matter.
DEPLOYMENT_SETTINGS: tuple[DeploymentSetting, ...] = (
    DeploymentSetting("platform.name", "general", "Platform name"),
    DeploymentSetting("platform.environment", "general", "Environment"),
    DeploymentSetting("platform.version", "general", "Version"),
    DeploymentSetting("notifications.email_logo_path", "branding", "Email logo file"),
    DeploymentSetting("logging.level", "general", "Log level"),
    DeploymentSetting("auth.access_token_ttl_seconds", "security", "Access token lifetime (s)"),
    DeploymentSetting("auth.refresh_token_ttl_seconds", "security", "Refresh token lifetime (s)"),
    DeploymentSetting("auth.jwt_secret_key", "security", "JWT signing key"),
    DeploymentSetting("security.encryption_key", "security", "Credential encryption key"),
    DeploymentSetting("security.argon2_time_cost", "security", "Password hashing time cost"),
    DeploymentSetting("agents.disabled_tool_types", "security", "Disabled tool types"),
    DeploymentSetting("agents.database_allowed_hosts", "security", "SQL tool hosts"),
    DeploymentSetting("agents.openapi_allowed_hosts", "security", "OpenAPI tool hosts"),
    DeploymentSetting("agents.database_max_rows", "security", "SQL tool row limit"),
    DeploymentSetting(
        "agents.database_statement_timeout_seconds", "security", "SQL tool timeout (s)"
    ),
    DeploymentSetting(
        "agents.approval_required_risk_levels", "security", "Risk levels needing approval"
    ),
    DeploymentSetting("airgap.enforced", "security", "Air-gap enforced"),
    DeploymentSetting("security.trusted_proxy_cidrs", "networking", "Trusted proxy networks"),
    DeploymentSetting("enrollment.allowed_agent_ports", "networking", "Allowed node agent ports"),
    DeploymentSetting(
        "enrollment.max_attempts_per_token", "networking", "Enrolment attempts per token"
    ),
    DeploymentSetting("oidc.enabled", "integrations", "OpenID Connect sign-in"),
    DeploymentSetting("ldap.enabled", "integrations", "LDAP / Active Directory sign-in"),
    DeploymentSetting("tracing.enabled", "integrations", "OpenTelemetry tracing"),
    DeploymentSetting("langfuse.enabled", "integrations", "Langfuse"),
    DeploymentSetting("ops.loki_url", "integrations", "Loki (support bundle logs)"),
)


# ---------------------------------------------------------------------------
# Applying overrides
# ---------------------------------------------------------------------------
def _group_model(settings: Settings, group: str) -> BaseModel:
    model = getattr(settings, group)
    if not isinstance(model, BaseModel):
        raise TypeError(f"settings.{group} is not a settings group")
    return model


def validate_override(settings: Settings, key: str, value: Any) -> Any:
    """The value as the settings model would hold it, or ``ValidationError``.

    Validated by rebuilding the group model with the new value, so the field's own type and
    bounds apply exactly as they do to the environment.
    """
    definition = BY_KEY.get(key)
    if definition is None:
        raise NotFoundError(f"No runtime setting named {key!r}.")
    group = _group_model(settings, definition.group)
    data = group.model_dump(by_alias=False)
    data[definition.field] = value
    try:
        rebuilt = type(group).model_validate(data)
    except PydanticValidationError as exc:
        problems = "; ".join(err["msg"] for err in exc.errors())
        raise ValidationError(f"{definition.label}: {problems}", details={"key": key}) from exc
    coerced = getattr(rebuilt, definition.field)
    if definition.check is not None:
        try:
            definition.check(coerced)
        except ValueError as exc:
            raise ValidationError(f"{definition.label} {exc}.", details={"key": key}) from exc
    return coerced


def apply_overrides(base: Settings, overrides: dict[str, Any]) -> tuple[Settings, dict[str, str]]:
    """``base`` with every valid override applied, and the problems with the invalid ones."""
    updates: dict[str, dict[str, Any]] = {}
    problems: dict[str, str] = {}
    for key, value in overrides.items():
        try:
            coerced = validate_override(base, key, value)
        except (ValidationError, NotFoundError) as exc:
            problems[key] = str(exc)
            continue
        definition = BY_KEY[key]
        updates.setdefault(definition.group, {})[definition.field] = coerced
    if not updates:
        return base, problems
    groups = {
        group: _group_model(base, group).model_copy(update=fields)
        for group, fields in updates.items()
    }
    return base.model_copy(update=groups), problems


class RuntimeSettingsCache:
    """The effective settings for this process; see the module docstring on propagation."""

    def __init__(self, ttl_seconds: float = CACHE_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._loaded_at: float | None = None
        self._base: Settings | None = None
        self._effective: Settings | None = None
        self._overrides: dict[str, Any] = {}
        self._problems: dict[str, str] = {}

    def invalidate(self) -> None:
        self._loaded_at = None

    def latest(self, base: Settings) -> Settings:
        """The last effective settings loaded, without touching the database.

        For construction paths that cannot await. Falls back to ``base`` before the first
        load, which is what the platform did before runtime settings existed.
        """
        if self._effective is not None and self._base is base:
            return self._effective
        return base

    async def effective(self, session: AsyncSession, base: Settings) -> Settings:
        fresh = (
            self._loaded_at is not None
            and self._base is base
            and time.monotonic() - self._loaded_at < self._ttl
        )
        if fresh and self._effective is not None:
            return self._effective
        overrides = await load_overrides(session)
        effective, problems = apply_overrides(base, overrides)
        for key, problem in problems.items():
            if self._problems.get(key) != problem:
                log.warning("runtime_setting_ignored", key=key, problem=problem)
        self._base, self._effective = base, effective
        self._overrides, self._problems = overrides, problems
        self._loaded_at = time.monotonic()
        return effective


#: One per process. Tests reset it between cases (``tests/conftest.py``).
RUNTIME = RuntimeSettingsCache()


async def load_overrides(session: AsyncSession) -> dict[str, Any]:
    rows = await session.execute(
        select(SystemSetting.key, SystemSetting.value).where(
            SystemSetting.key.startswith(KEY_PREFIX)
        )
    )
    return {key.removeprefix(KEY_PREFIX): value for key, value in rows.all()}


# ---------------------------------------------------------------------------
# The service behind /settings
# ---------------------------------------------------------------------------
def _default_of(settings: Settings, group: str, field: str) -> Any:
    info = type(_group_model(settings, group)).model_fields[field]
    if info.default_factory is not None:
        return info.default_factory()  # type: ignore[call-arg]
    return info.default


def _display(key: str, value: Any) -> Any:
    if is_secret_key(key.split(".", 1)[1]):
        return REDACTED if value else None
    return redact_value(value)


class RuntimeSettingsService:
    def __init__(
        self,
        session: AsyncSession,
        base: Settings,
        audit: AuditService,
        cache: RuntimeSettingsCache = RUNTIME,
    ) -> None:
        self._session = session
        self._base = base
        self._audit = audit
        self._cache = cache

    async def catalogue(self) -> dict[str, Any]:
        rows = {
            row.key.removeprefix(KEY_PREFIX): row
            for row in (
                await self._session.execute(
                    select(SystemSetting).where(SystemSetting.key.startswith(KEY_PREFIX))
                )
            ).scalars()
        }
        _effective, problems = apply_overrides(
            self._base, {key: row.value for key, row in rows.items()}
        )
        items: list[dict[str, Any]] = []
        for definition in RUNTIME_SETTINGS:
            configured = getattr(_group_model(self._base, definition.group), definition.field)
            default = _default_of(self._base, definition.group, definition.field)
            row = rows.get(definition.key)
            valid_override = row is not None and definition.key not in problems
            items.append(
                {
                    "key": definition.key,
                    "category": definition.category,
                    "label": definition.label,
                    "description": definition.description,
                    "editable": True,
                    "applies": definition.applies,
                    "env_var": definition.env_var,
                    "type": type(configured).__name__,
                    "value": validate_override(self._base, definition.key, row.value)
                    if valid_override and row is not None
                    else configured,
                    "configured_value": configured,
                    "default_value": default,
                    "source": "override"
                    if valid_override
                    else ("default" if configured == default else "configured"),
                    "problem": problems.get(definition.key),
                    "updated_at": row.updated_at if row is not None else None,
                }
            )
        for deployment in DEPLOYMENT_SETTINGS:
            group, field = deployment.key.split(".", 1)
            value = getattr(_group_model(self._base, group), field)
            default = _default_of(self._base, group, field)
            items.append(
                {
                    "key": deployment.key,
                    "category": deployment.category,
                    "label": deployment.label,
                    "description": None,
                    "editable": False,
                    "applies": "Restart",
                    "env_var": deployment.env_var,
                    "type": type(value).__name__,
                    "value": _display(deployment.key, value),
                    "configured_value": None,
                    "default_value": None,
                    "source": "default" if _safe_equal(value, default) else "configured",
                    "problem": None,
                    "updated_at": None,
                }
            )
        return {
            "categories": [{"id": cid, "label": label} for cid, label in CATEGORIES],
            "items": items,
            "propagation_seconds": int(self._cache._ttl),
        }

    async def update(self, key: str, value: Any, *, actor: User, reason: str | None) -> None:
        definition = BY_KEY.get(key)
        if definition is None:
            raise NotFoundError(f"No runtime setting named {key!r}.")
        coerced = validate_override(self._base, key, value)
        row = await self._session.get(SystemSetting, KEY_PREFIX + key)
        previous = row.value if row is not None else None
        if row is None:
            row = SystemSetting(
                key=KEY_PREFIX + key,
                value=coerced,
                category=definition.category,
                description=definition.label,
                is_system=False,
            )
            self._session.add(row)
        else:
            row.value = coerced
        await self._session.flush()
        await self._audit.record(
            AuditAction.SETTING_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="setting",
            resource_id=key,
            message=f"{key} set to {coerced!r}",
            metadata={"key": key, "previous": previous, "value": coerced, "reason": reason},
        )
        self._cache.invalidate()
        log.info("runtime_setting_updated", key=key, user=actor.username)

    async def reset(self, key: str, *, actor: User, reason: str | None) -> None:
        if key not in BY_KEY:
            raise NotFoundError(f"No runtime setting named {key!r}.")
        row = await self._session.get(SystemSetting, KEY_PREFIX + key)
        if row is None:
            return
        previous = row.value
        await self._session.execute(
            delete(SystemSetting).where(SystemSetting.key == KEY_PREFIX + key)
        )
        await self._audit.record(
            AuditAction.SETTING_RESET,
            user_id=actor.id,
            username=actor.username,
            resource_type="setting",
            resource_id=key,
            message=f"{key} reset to the deployment value",
            metadata={"key": key, "previous": previous, "reason": reason},
        )
        self._cache.invalidate()
        log.info("runtime_setting_reset", key=key, user=actor.username)

    async def commit(self) -> None:
        await self._session.commit()


def _safe_equal(left: Any, right: Any) -> bool:
    try:
        return bool(left == right)
    except Exception:
        return False
