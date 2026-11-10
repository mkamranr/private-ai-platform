"""Managing gateway quotas (E2, plan §5.14).

Enforcement lives in app/services/gateway_policy.py; this is the operator side: create,
change and remove limits, and show how much of each has been used.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.operations import QUOTA_WILDCARD, GatewayQuota, QuotaScope
from app.repositories.models_registry import ApiKeyRepository, ModelRepository
from app.repositories.operations import GatewayQuotaRepository, InferenceBackendRepository
from app.repositories.teams import TeamRepository
from app.services.audit import AuditService
from app.services.gateway_policy import BREAKERS, GatewayPolicy

log = get_logger(__name__)

_LIMITS = ("tokens_per_minute", "daily_tokens", "monthly_tokens", "max_concurrency")


class QuotaService:
    def __init__(
        self,
        quotas: GatewayQuotaRepository,
        keys: ApiKeyRepository,
        models: ModelRepository,
        backends: InferenceBackendRepository,
        policy: GatewayPolicy,
        audit: AuditService,
        teams: TeamRepository | None = None,
    ) -> None:
        self._quotas = quotas
        self._keys = keys
        self._models = models
        self._backends = backends
        self._policy = policy
        self._audit = audit
        self._teams = teams

    # -- reads -------------------------------------------------------------
    async def list_quotas(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        if scope is not None and scope not in set(QuotaScope):
            raise ValidationError(
                f"Unknown scope {scope!r}.", details={"allowed": sorted(QuotaScope)}
            )
        return [await self.describe(q) for q in await self._quotas.list_quotas(scope=scope)]

    async def describe(self, quota: GatewayQuota) -> dict[str, Any]:
        """A quota with its subject named and its current usage."""
        return {
            "id": quota.id,
            "scope": quota.scope,
            "subject": quota.subject,
            "subject_label": await self._label(quota),
            "description": quota.description,
            "tokens_per_minute": quota.tokens_per_minute,
            "daily_tokens": quota.daily_tokens,
            "monthly_tokens": quota.monthly_tokens,
            "max_concurrency": quota.max_concurrency,
            "enabled": quota.enabled,
            "usage": await self._policy.usage_for(quota),
            "created_at": quota.created_at,
            "updated_at": quota.updated_at,
        }

    async def get(self, quota_id: uuid.UUID) -> GatewayQuota:
        quota = await self._quotas.get(quota_id)
        if quota is None:
            raise NotFoundError(f"No quota with id {quota_id}.")
        return quota

    async def circuits(self) -> list[dict[str, Any]]:
        """This replica's circuit breakers, with the backend or deployment named."""
        rows = BREAKERS.snapshot()
        for row in rows:
            kind, _, ident = row["target"].partition(":")
            row["label"] = None
            if kind == "backend":
                try:
                    backend = await self._backends.get(uuid.UUID(ident))
                except ValueError:
                    backend = None
                row["label"] = f"backend {backend.name}" if backend else None
            elif kind == "deployment":
                row["label"] = f"deployment {ident[:8]}"
        return rows

    # -- writes ------------------------------------------------------------
    async def create(self, *, actor: User, **fields: Any) -> GatewayQuota:
        scope, subject = fields["scope"], fields["subject"].strip()
        self._require_a_limit(fields)
        await self._check_subject(scope, subject)
        if await self._quotas.get_by_subject(scope, subject) is not None:
            raise ConflictError(
                f"A {scope} quota for {subject!r} already exists — change that one instead.",
                details={"field": "subject"},
            )

        quota = GatewayQuota(
            scope=scope,
            subject=subject,
            description=fields.get("description"),
            enabled=fields.get("enabled", True),
            created_by=actor.id,
            **{name: fields.get(name) for name in _LIMITS},
        )
        self._quotas.add(quota)
        await self._quotas.flush()
        await self._quotas.session.refresh(quota)
        await self._audit.record(
            AuditAction.QUOTA_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="gateway_quota",
            resource_id=str(quota.id),
            metadata=self._snapshot(quota),
        )
        log.info("quota_created", scope=scope, subject=subject)
        return quota

    async def update(
        self, quota_id: uuid.UUID, changes: dict[str, Any], *, actor: User
    ) -> GatewayQuota:
        quota = await self.get(quota_id)
        before = self._snapshot(quota)
        merged = {name: changes.get(name, getattr(quota, name)) for name in _LIMITS}
        self._require_a_limit(merged)

        for name in _LIMITS:
            if name in changes:
                setattr(quota, name, changes[name])
        if "description" in changes:
            quota.description = changes["description"]
        if changes.get("enabled") is not None:
            quota.enabled = changes["enabled"]

        await self._quotas.flush()
        await self._quotas.session.refresh(quota)
        await self._audit.record(
            AuditAction.QUOTA_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="gateway_quota",
            resource_id=str(quota.id),
            metadata={"before": before, "after": self._snapshot(quota)},
        )
        return quota

    async def delete(self, quota_id: uuid.UUID, *, actor: User) -> None:
        quota = await self.get(quota_id)
        snapshot = self._snapshot(quota)
        await self._quotas.delete(quota)
        await self._audit.record(
            AuditAction.QUOTA_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="gateway_quota",
            resource_id=str(quota_id),
            metadata=snapshot,
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _require_a_limit(fields: dict[str, Any]) -> None:
        if all(fields.get(name) is None for name in _LIMITS):
            raise ValidationError(
                "Set at least one limit: tokens per minute, a daily or monthly token "
                "budget, or a concurrency limit.",
                details={"fields": list(_LIMITS)},
            )

    async def _check_subject(self, scope: str, subject: str) -> None:
        """Catch typos at creation, when they are cheap — a quota on a key id or model
        name that does not exist would silently limit nothing."""
        if subject == QUOTA_WILDCARD or scope == QuotaScope.END_USER:
            return
        if scope == QuotaScope.API_KEY:
            try:
                key = await self._keys.get(uuid.UUID(subject))
            except ValueError:
                key = None
            if key is None:
                raise ValidationError(
                    f"No API key with id {subject!r}. Use the key's id, not its secret or prefix.",
                    details={"field": "subject"},
                )
        elif scope == QuotaScope.TEAM:
            try:
                team = await self._teams.get(uuid.UUID(subject)) if self._teams else None
            except ValueError:
                team = None
            if team is None:
                raise ValidationError(
                    f"No team with id {subject!r}. Team quotas use the team's id.",
                    details={"field": "subject"},
                )
        elif scope == QuotaScope.MODEL and await self._models.get_by_name(subject) is None:
            raise ValidationError(
                f"No model named {subject!r}. Model quotas use the model's name (the model "
                "that answers), not an alias.",
                details={"field": "subject"},
            )

    async def _label(self, quota: GatewayQuota) -> str | None:
        if quota.subject == QUOTA_WILDCARD:
            return {
                QuotaScope.API_KEY: "every API key",
                QuotaScope.END_USER: "every trusted end user",
                QuotaScope.MODEL: "every model",
                QuotaScope.TEAM: "every team",
            }.get(QuotaScope(quota.scope))
        if quota.scope == QuotaScope.API_KEY:
            try:
                key = await self._keys.get(uuid.UUID(quota.subject))
            except ValueError:
                return None
            return f"{key.name} ({key.prefix}…)" if key else "deleted key"
        if quota.scope == QuotaScope.TEAM and self._teams is not None:
            try:
                team = await self._teams.get(uuid.UUID(quota.subject))
            except ValueError:
                return None
            return f"team {team.name}" if team else "deleted team"
        return quota.subject

    @staticmethod
    def _snapshot(quota: GatewayQuota) -> dict[str, Any]:
        return {
            "scope": quota.scope,
            "subject": quota.subject,
            "enabled": quota.enabled,
            **{name: getattr(quota, name) for name in _LIMITS},
        }
