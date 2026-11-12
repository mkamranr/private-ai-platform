"""Budget alerts (E12): warnings before a limit is reached, not only refusals after.

Two kinds of budget, watched by one worker job (``InfrastructureWorker.check_budgets``):

* **Token budgets** are the daily and monthly limits on gateway quotas (E2). The gateway
  already refuses a call once one is used up. The watcher raises a WARNING alert at
  ``gateway.budget_warning_percent`` of the limit and a CRITICAL one at 100%, so someone
  hears about it before users start getting 429s. For a ``*`` quota, each subject it
  governs is watched separately, skipping subjects that have an enabled quota of their
  own (the gateway applies that one instead).
* **Cost budgets** (``cost_budgets``) cap the chargeback estimate for the calendar month,
  overall, per team or per API key. They raise the same two alerts and **never refuse
  traffic**: the estimate depends on rates an operator typed in, which is not a sound
  basis for turning requests away.

Alerts clear themselves when the condition goes away. That happens when a period rolls
over, a limit is raised or a budget is removed, so nobody has to resolve a "budget
exceeded" alert from last month by hand.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.scope import Scope
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.chargeback import GLOBAL_BUDGET_SUBJECT, CostBudget, CostBudgetScope
from app.models.operations import QUOTA_WILDCARD, AlertSeverity, GatewayQuota, QuotaScope
from app.repositories.chargeback import CostBudgetRepository
from app.repositories.models_registry import ApiKeyRepository
from app.repositories.operations import AlertRepository, GatewayQuotaRepository
from app.repositories.teams import TeamRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService
from app.services.chargeback import ChargebackService

log = get_logger(__name__)

QUOTA_ALERT_SOURCE = "quota_budget"
COST_ALERT_SOURCE = "cost_budget"
_HUNDRED = Decimal(100)


def quota_budget_fingerprint(quota_id: uuid.UUID, period: str, subject: str) -> str:
    return f"{QUOTA_ALERT_SOURCE}:{quota_id}:{period}:{subject}"


def cost_budget_fingerprint(budget_id: uuid.UUID) -> str:
    return f"{COST_ALERT_SOURCE}:{budget_id}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def level(used: Decimal, limit: Decimal, warn_percent: int) -> AlertSeverity | None:
    """CRITICAL at or past the limit, WARNING at ``warn_percent`` of it, else None."""
    if limit <= 0:
        return None
    if used >= limit:
        return AlertSeverity.CRITICAL
    if used * _HUNDRED >= limit * warn_percent:
        return AlertSeverity.WARNING
    return None


def _percent(used: Decimal, limit: Decimal) -> int:
    return int(used * _HUNDRED / limit) if limit > 0 else 0


class _Labels:
    """Names for alert titles and the budgets list. Cached for one run."""

    def __init__(self, keys: ApiKeyRepository, teams: TeamRepository) -> None:
        self._keys = keys
        self._teams = teams
        self._cache: dict[tuple[str, str], str] = {}

    async def __call__(self, scope: str, subject: str) -> str:
        cached = self._cache.get((scope, subject))
        if cached is not None:
            return cached
        label = await self._resolve(scope, subject)
        self._cache[(scope, subject)] = label
        return label

    async def _resolve(self, scope: str, subject: str) -> str:
        if subject in (QUOTA_WILDCARD, GLOBAL_BUDGET_SUBJECT) and scope == CostBudgetScope.GLOBAL:
            return "whole platform"
        if scope in (QuotaScope.API_KEY, CostBudgetScope.API_KEY):
            try:
                key = await self._keys.get(uuid.UUID(subject))
            except ValueError:
                key = None
            return (
                f"API key {key.name} ({key.prefix}…)" if key else f"deleted API key {subject[:8]}"
            )
        if scope in (QuotaScope.TEAM, CostBudgetScope.TEAM):
            try:
                team = await self._teams.get(uuid.UUID(subject))
            except ValueError:
                team = None
            return f"team {team.name}" if team else f"deleted team {subject[:8]}"
        if scope == QuotaScope.END_USER:
            return f"user {subject}"
        return f"model {subject}"


# ---------------------------------------------------------------------------
# Cost budgets
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Spend:
    """Month-to-date spend against one budget."""

    amount: Decimal | None
    #: Some of the subject's usage had no rate, so ``amount`` is a lower bound.
    partial: bool = False


class CostBudgetService:
    def __init__(
        self,
        budgets: CostBudgetRepository,
        chargeback: ChargebackService,
        keys: ApiKeyRepository,
        teams: TeamRepository,
        audit: AuditService,
    ) -> None:
        self._budgets = budgets
        self._chargeback = chargeback
        self._keys = keys
        self._teams = teams
        self._audit = audit
        self._labels = _Labels(keys, teams)

    @property
    def currency(self) -> str:
        return self._chargeback.currency

    async def commit(self) -> None:
        await self._budgets.session.commit()

    # -- reads ---------------------------------------------------------------
    @staticmethod
    def visible(budget: CostBudget, scope: Scope) -> bool:
        """Team budgets follow team scope. Global and API key budgets reveal spend
        across teams, so they need ``scope.all``."""
        if scope.all_teams:
            return True
        if budget.scope != CostBudgetScope.TEAM:
            return False
        try:
            return scope.can_see(uuid.UUID(budget.subject))
        except ValueError:
            return False

    async def list_budgets(self, *, scope: Scope, now: dt.datetime | None = None) -> list[dict]:
        now = now or _now()
        budgets = [b for b in await self._budgets.list_all() if self.visible(b, scope)]
        spend = await self.month_to_date(budgets, now=now)
        return [await self.describe(b, spend[b.id]) for b in budgets]

    async def get(self, budget_id: uuid.UUID, *, scope: Scope) -> CostBudget:
        budget = await self._budgets.get(budget_id)
        if budget is None or not self.visible(budget, scope):
            raise NotFoundError(f"No cost budget with id {budget_id}.")
        return budget

    async def describe(self, budget: CostBudget, spend: Spend) -> dict[str, Any]:
        percent = (
            _percent(spend.amount, budget.monthly_amount) if spend.amount is not None else None
        )
        status = "unpriced"
        if spend.amount is not None:
            found = level(spend.amount, budget.monthly_amount, budget.warn_percent)
            status = (
                "exceeded"
                if found == AlertSeverity.CRITICAL
                else "warning"
                if found == AlertSeverity.WARNING
                else "ok"
            )
        return {
            "id": budget.id,
            "scope": budget.scope,
            "subject": budget.subject,
            "subject_label": await self._labels(budget.scope, budget.subject),
            "monthly_amount": budget.monthly_amount,
            "warn_percent": budget.warn_percent,
            "description": budget.description,
            "enabled": budget.enabled,
            "spend": spend.amount,
            "spend_partial": spend.partial,
            "percent": percent,
            "status": status if budget.enabled else "disabled",
            "currency": self.currency,
            "created_at": budget.created_at,
            "updated_at": budget.updated_at,
        }

    async def month_to_date(
        self, budgets: Sequence[CostBudget], *, now: dt.datetime
    ) -> dict[uuid.UUID, Spend]:
        """Spend this calendar month (UTC) per budget, from one report per grouping."""
        if not budgets:
            return {}
        start, end = now.date().replace(day=1), now.date()
        reports: dict[str, dict[str, Any]] = {}
        for group in sorted({self._group(b) for b in budgets}):
            reports[group] = await self._chargeback.report(
                group=group, start=start, end=end, scope=Scope.everything(), now=now, limit=None
            )
        out: dict[uuid.UUID, Spend] = {}
        for budget in budgets:
            report = reports[self._group(budget)]
            if budget.scope == CostBudgetScope.GLOBAL:
                totals = report["totals"]
                out[budget.id] = Spend(totals["total_cost"], bool(totals["unpriced_requests"]))
                continue
            row = next((r for r in report["rows"] if r["key"] == budget.subject), None)
            if row is None:
                out[budget.id] = Spend(Decimal(0))  # no usage yet this month
            else:
                out[budget.id] = Spend(row["total_cost"], bool(row["unpriced_requests"]))
        return out

    @staticmethod
    def _group(budget: CostBudget) -> str:
        return "api_key" if budget.scope == CostBudgetScope.API_KEY else "team"

    # -- writes --------------------------------------------------------------
    async def create(
        self,
        *,
        actor: User,
        scope: Scope,
        budget_scope: str,
        subject: str | None,
        monthly_amount: Decimal,
        warn_percent: int,
        description: str | None,
        enabled: bool,
    ) -> CostBudget:
        subject = await self._check_subject(budget_scope, subject)
        budget = CostBudget(
            scope=budget_scope,
            subject=subject,
            monthly_amount=monthly_amount,
            warn_percent=warn_percent,
            description=description,
            enabled=enabled,
            created_by=actor.id,
        )
        if not self.visible(budget, scope):
            raise NotFoundError("Only holders of scope.all can set this budget.")
        if await self._budgets.get_by_subject(budget_scope, subject) is not None:
            raise ConflictError(
                "That budget already exists; change it instead.", details={"field": "subject"}
            )
        self._budgets.add(budget)
        await self._budgets.flush()
        await self._budgets.session.refresh(budget)
        await self._audit.record(
            AuditAction.COST_BUDGET_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="cost_budget",
            resource_id=str(budget.id),
            metadata=self._snapshot(budget),
        )
        return budget

    async def update(
        self, budget_id: uuid.UUID, changes: dict[str, Any], *, actor: User, scope: Scope
    ) -> CostBudget:
        budget = await self.get(budget_id, scope=scope)
        before = self._snapshot(budget)
        for name in ("monthly_amount", "warn_percent", "description", "enabled"):
            if name in changes and (changes[name] is not None or name == "description"):
                setattr(budget, name, changes[name])
        await self._budgets.flush()
        await self._budgets.session.refresh(budget)
        await self._audit.record(
            AuditAction.COST_BUDGET_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="cost_budget",
            resource_id=str(budget.id),
            metadata={"before": before, "after": self._snapshot(budget)},
        )
        return budget

    async def delete(self, budget_id: uuid.UUID, *, actor: User, scope: Scope) -> None:
        budget = await self.get(budget_id, scope=scope)
        snapshot = self._snapshot(budget)
        await self._budgets.delete(budget)
        await self._audit.record(
            AuditAction.COST_BUDGET_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="cost_budget",
            resource_id=str(budget_id),
            metadata=snapshot,
        )

    async def _check_subject(self, budget_scope: str, subject: str | None) -> str:
        if budget_scope not in set(CostBudgetScope):
            raise ValidationError(
                f"Unknown budget scope {budget_scope!r}.",
                details={"field": "scope", "allowed": sorted(CostBudgetScope)},
            )
        if budget_scope == CostBudgetScope.GLOBAL:
            return GLOBAL_BUDGET_SUBJECT
        try:
            ident = uuid.UUID((subject or "").strip())
        except ValueError:
            raise ValidationError(
                "Give the team's or API key's id.", details={"field": "subject"}
            ) from None
        found = (
            await self._teams.get(ident)
            if budget_scope == CostBudgetScope.TEAM
            else await self._keys.get(ident)
        )
        if found is None:
            what = "team" if budget_scope == CostBudgetScope.TEAM else "API key"
            raise ValidationError(f"No {what} with id {ident}.", details={"field": "subject"})
        return str(ident)

    @staticmethod
    def _snapshot(budget: CostBudget) -> dict[str, Any]:
        return {
            "scope": budget.scope,
            "subject": budget.subject,
            "monthly_amount": str(budget.monthly_amount),
            "warn_percent": budget.warn_percent,
            "enabled": budget.enabled,
        }


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class WatchResult:
    raised: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    open: set[str] = field(default_factory=set)


class BudgetWatcher:
    """Compares every budget with usage and raises or clears alerts. Never refuses."""

    def __init__(
        self,
        quotas: GatewayQuotaRepository,
        budgets: CostBudgetRepository,
        costs: CostBudgetService,
        alerts: AlertService,
        alert_repo: AlertRepository,
        keys: ApiKeyRepository,
        teams: TeamRepository,
        *,
        warn_percent: int,
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self._quotas = quotas
        self._budgets = budgets
        self._costs = costs
        self._alerts = alerts
        self._alert_repo = alert_repo
        self._labels = _Labels(keys, teams)
        self._warn = warn_percent
        self._clock = clock

    async def run(self) -> WatchResult:
        result = WatchResult()
        await self._watch_quotas(result)
        await self._watch_costs(result)
        return result

    # -- token budgets -------------------------------------------------------
    async def _watch_quotas(self, result: WatchResult) -> None:
        now = self._clock()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)
        quotas = [q for q in await self._quotas.list_quotas() if q.enabled]
        explicit: dict[str, set[str]] = {}
        for quota in quotas:
            if quota.subject != QUOTA_WILDCARD:
                explicit.setdefault(quota.scope, set()).add(quota.subject)

        wanted: set[str] = set()
        by_subject: dict[tuple[str, dt.datetime], dict[str, int]] = {}
        for quota in quotas:
            periods = (
                ("daily", quota.daily_tokens, day, "today", "00:00 UTC"),
                ("monthly", quota.monthly_tokens, month, "this month", "the 1st (UTC)"),
            )
            for period, limit, since, when, resets in periods:
                if not limit:
                    continue
                if quota.subject == QUOTA_WILDCARD:
                    key = (quota.scope, since)
                    if key not in by_subject:
                        by_subject[key] = await self._quotas.tokens_by_subject_since(
                            quota.scope, since
                        )
                    usage = {
                        s: t
                        for s, t in by_subject[key].items()
                        if s not in explicit.get(quota.scope, set())
                    }
                else:
                    usage = {
                        quota.subject: await self._quotas.tokens_since(
                            quota.scope, quota.subject, since
                        )
                    }
                for subject, used in usage.items():
                    severity = level(Decimal(used), Decimal(limit), self._warn)
                    if severity is None:
                        continue
                    fingerprint = quota_budget_fingerprint(quota.id, period, subject)
                    wanted.add(fingerprint)
                    await self._raise_quota(
                        quota, fingerprint, severity, period, subject, used, limit, when, resets
                    )
                    result.raised.append(fingerprint)
        await self._clear_unwanted(QUOTA_ALERT_SOURCE, wanted, result)

    async def _raise_quota(
        self,
        quota: GatewayQuota,
        fingerprint: str,
        severity: AlertSeverity,
        period: str,
        subject: str,
        used: int,
        limit: int,
        when: str,
        resets: str,
    ) -> None:
        who = await self._labels(quota.scope, subject)
        percent = _percent(Decimal(used), Decimal(limit))
        if severity == AlertSeverity.CRITICAL:
            title = f"{period.capitalize()} token budget used up: {who}"
            tail = f"The gateway refuses further requests until it resets at {resets}."
        else:
            title = f"{period.capitalize()} token budget {percent}% used: {who}"
            tail = f"At 100% the gateway refuses requests until {resets}."
        await self._alerts.raise_alert(
            fingerprint=fingerprint,
            severity=severity,
            source=QUOTA_ALERT_SOURCE,
            title=title,
            message=f"{used:,} of {limit:,} tokens {when}. {tail}",
            resource_type="gateway_quota",
            resource_id=str(quota.id),
        )

    # -- cost budgets --------------------------------------------------------
    async def _watch_costs(self, result: WatchResult) -> None:
        now = self._clock()
        budgets = list(await self._budgets.list_all(enabled_only=True))
        spend = await self._costs.month_to_date(budgets, now=now)
        wanted: set[str] = set()
        for budget in budgets:
            amount = spend[budget.id].amount
            if amount is None:
                continue  # nothing priced: no estimate to compare
            severity = level(amount, budget.monthly_amount, budget.warn_percent)
            if severity is None:
                continue
            fingerprint = cost_budget_fingerprint(budget.id)
            wanted.add(fingerprint)
            who = await self._labels(budget.scope, budget.subject)
            percent = _percent(amount, budget.monthly_amount)
            currency = self._costs.currency
            title = (
                f"Cost budget exceeded: {who}"
                if severity == AlertSeverity.CRITICAL
                else f"Cost budget {percent}% used: {who}"
            )
            note = (
                " Some usage has no rate, so the real figure is higher."
                if spend[budget.id].partial
                else ""
            )
            await self._alerts.raise_alert(
                fingerprint=fingerprint,
                severity=severity,
                source=COST_ALERT_SOURCE,
                title=title,
                message=(
                    f"Estimated {amount:.2f} of {budget.monthly_amount:.2f} {currency} this "
                    f"month (UTC). Budgets raise alerts and never block traffic.{note}"
                ),
                resource_type="cost_budget",
                resource_id=str(budget.id),
            )
            result.raised.append(fingerprint)
        await self._clear_unwanted(COST_ALERT_SOURCE, wanted, result)

    async def _clear_unwanted(self, source: str, wanted: set[str], result: WatchResult) -> None:
        for fingerprint in await self._alert_repo.open_fingerprints(source) - wanted:
            if await self._alerts.clear(fingerprint):
                result.cleared.append(fingerprint)
        result.open |= wanted
