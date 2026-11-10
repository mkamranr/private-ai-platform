"""Gateway policy (expansion E2, plan §5.5 and §5.14).

Three controls that sit in front of a model, each answering a different question:

* **Token budgets** — "has this caller used their allowance?" Per minute, per UTC day and
  per UTC month, for an API key, a trusted end user, or a model. Counted in Valkey so the
  check costs one round trip, and seeded from ``usage_records`` whenever a counter is
  missing. The database stays the source of truth: a Valkey restart, a new day or a quota
  created mid-month all start from what was actually billed, never from zero.
* **Concurrency limits** — "how many of this caller's requests are in flight?" A
  sorted set of leases per subject. A crashed replica cannot release its slots, so each
  lease lapses after ``concurrency_lease_seconds`` instead of shrinking the limit forever.
* **Circuit breakers** — "is this backend already known to be down?" After repeated
  upstream failures a backend's circuit opens and calls fail fast (or go to the alias's
  explicit fallback) instead of every caller waiting out its own timeout.

Two rules shape every method here:

1. **Budgets are checked before a call and charged after it.** The cost of a completion
   is unknown until it finishes, so the call that crosses a budget is allowed to finish
   and the next one is refused. Overshoot is bounded by one request per subject.
2. **Valkey failing never blocks inference**, matching the per-key rate limiter. Budget
   checks fall back to summing ``usage_records`` directly; concurrency and per-minute
   limits are skipped and the failure logged. A soft control must not become a hard
   outage.
"""

from __future__ import annotations

import datetime as dt
import math
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from app.config.settings import GatewaySettings
from app.core import metrics
from app.core.errors import RateLimitError
from app.core.logging import get_logger
from app.models.operations import GatewayQuota, QuotaScope
from app.repositories.operations import GatewayQuotaRepository

log = get_logger(__name__)

#: Increment a counter only if it already exists. A missing day/month counter must be
#: *seeded from the database*, not created by an increment — creating it here would
#: start the budget at this one request's tokens and forget everything earlier.
_INCR_IF_EXISTS = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return redis.call('INCRBY', KEYS[1], ARGV[1])
end
return false
"""

_DAY_TTL = 2 * 86_400
_MONTH_TTL = 32 * 86_400


@dataclass(slots=True)
class _Charge:
    scope: str
    subject: str
    quota: GatewayQuota


@dataclass(slots=True)
class _Slot:
    scope: str
    key: str
    member: str


@dataclass(slots=True)
class Admission:
    """What one request was admitted against, so it can be charged and released."""

    charges: list[_Charge] = field(default_factory=list)
    slots: list[_Slot] = field(default_factory=list)


def _describe(scope: str, subject: str) -> str:
    if scope == QuotaScope.API_KEY:
        return "this API key"
    if scope == QuotaScope.END_USER:
        return f"user {subject!r}"
    if scope == QuotaScope.TEAM:
        return "this key's team"
    return f"model {subject!r}"


def _start_of_day(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month(now: dt.datetime) -> dt.datetime:
    return _start_of_day(now).replace(day=1)


def _next_month(now: dt.datetime) -> dt.datetime:
    first = _start_of_month(now)
    return (first + dt.timedelta(days=32)).replace(day=1)


class GatewayPolicy:
    """Budgets and concurrency for one request. Built per request; holds no state."""

    def __init__(
        self,
        settings: GatewaySettings,
        redis: Redis,
        quotas: GatewayQuotaRepository,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._quotas = quotas
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    # -- admission ---------------------------------------------------------
    async def admit(self, admission: Admission, subjects: Sequence[tuple[str, str]]) -> None:
        """Check budgets and take concurrency slots for each subject, or refuse.

        On refusal nothing taken *here* is left held. Slots taken by an earlier call on
        the same admission stay recorded on it; the caller releases the admission.
        """
        quotas = await self._quotas.applicable(subjects)
        if not quotas:
            return
        subject_of = dict(subjects)
        taken: list[_Slot] = []
        try:
            for scope, quota in quotas.items():
                subject = subject_of[scope]
                if quota.has_token_limits:
                    await self._check_budgets(scope, subject, quota)
                    admission.charges.append(_Charge(scope, subject, quota))
                if quota.max_concurrency:
                    slot = await self._acquire(scope, subject, quota.max_concurrency)
                    if slot is not None:
                        taken.append(slot)
                        admission.slots.append(slot)
        except RateLimitError:
            await self._release_slots(taken)
            for slot in taken:
                admission.slots.remove(slot)
            admission.charges = [c for c in admission.charges if c.scope not in quotas]
            raise

    async def drop_scope(self, admission: Admission, scope: str) -> None:
        """Forget one scope's charges and slots — used when a fallback model takes over,
        so the model that did not answer is neither charged nor kept busy."""
        slots = [s for s in admission.slots if s.scope == scope]
        await self._release_slots(slots)
        admission.slots = [s for s in admission.slots if s.scope != scope]
        admission.charges = [c for c in admission.charges if c.scope != scope]

    async def release(self, admission: Admission) -> None:
        """Give back every concurrency slot. Idempotent."""
        slots, admission.slots = admission.slots, []
        await self._release_slots(slots)

    async def charge(self, admission: Admission, tokens: int) -> None:
        """Add a finished call's tokens to every budget it was admitted against.

        Runs after the usage record is committed, so a counter seeded concurrently from
        the database cannot count this call twice.
        """
        if tokens <= 0 or not admission.charges:
            return
        now = self._clock()
        for charge in admission.charges:
            keys = _keys(charge.scope, charge.subject, now)
            try:
                if charge.quota.tokens_per_minute:
                    pipe = self._redis.pipeline(transaction=False)
                    pipe.incrby(keys["minute"], tokens)
                    pipe.expire(keys["minute"], 120)
                    await pipe.execute()
                if charge.quota.daily_tokens:
                    await self._redis.eval(_INCR_IF_EXISTS, 1, keys["day"], tokens)
                if charge.quota.monthly_tokens:
                    await self._redis.eval(_INCR_IF_EXISTS, 1, keys["month"], tokens)
            except Exception as exc:
                # The usage row is already committed, so the next seed recovers this.
                log.warning("quota_charge_failed", scope=charge.scope, error=type(exc).__name__)

    # -- usage reporting ---------------------------------------------------
    async def usage_for(self, quota: GatewayQuota) -> dict[str, int | None]:
        """Current consumption against a quota, from the database (the source of truth).

        ``*`` rows have no single subject, so they report nothing.
        """
        if quota.subject == "*":
            return {"minute": None, "day": None, "month": None}
        now = self._clock()
        day = await self._quotas.tokens_since(quota.scope, quota.subject, _start_of_day(now))
        month = await self._quotas.tokens_since(quota.scope, quota.subject, _start_of_month(now))
        minute: int | None = None
        try:
            raw = await self._redis.get(_keys(quota.scope, quota.subject, now)["minute"])
            minute = int(raw) if raw is not None else 0
        except Exception:
            minute = None
        return {"minute": minute, "day": day, "month": month}

    # -- internals ---------------------------------------------------------
    async def _check_budgets(self, scope: str, subject: str, quota: GatewayQuota) -> None:
        now = self._clock()
        keys = _keys(scope, subject, now)
        who = _describe(scope, subject)

        if quota.tokens_per_minute:
            try:
                raw = await self._redis.get(keys["minute"])
            except Exception as exc:
                log.warning("quota_minute_unavailable", error=type(exc).__name__)
                raw = None
            if raw is not None and int(raw) >= quota.tokens_per_minute:
                self._refuse(
                    scope,
                    "tokens_per_minute",
                    f"Token rate limit of {quota.tokens_per_minute:,} tokens/minute "
                    f"reached for {who}.",
                    limit=quota.tokens_per_minute,
                    retry_after=60 - now.second,
                )

        if quota.daily_tokens:
            used = await self._budget_used(
                keys["day"], scope, subject, _start_of_day(now), _DAY_TTL
            )
            if used >= quota.daily_tokens:
                tomorrow = _start_of_day(now) + dt.timedelta(days=1)
                self._refuse(
                    scope,
                    "daily_tokens",
                    f"Daily token budget of {quota.daily_tokens:,} used up for {who}. "
                    "It resets at 00:00 UTC.",
                    limit=quota.daily_tokens,
                    used=used,
                    retry_after=math.ceil((tomorrow - now).total_seconds()),
                )

        if quota.monthly_tokens:
            used = await self._budget_used(
                keys["month"], scope, subject, _start_of_month(now), _MONTH_TTL
            )
            if used >= quota.monthly_tokens:
                self._refuse(
                    scope,
                    "monthly_tokens",
                    f"Monthly token budget of {quota.monthly_tokens:,} used up for {who}. "
                    "It resets on the 1st at 00:00 UTC.",
                    limit=quota.monthly_tokens,
                    used=used,
                    retry_after=math.ceil((_next_month(now) - now).total_seconds()),
                )

    async def _budget_used(
        self, key: str, scope: str, subject: str, since: dt.datetime, ttl: int
    ) -> int:
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            log.warning("quota_counter_unavailable", error=type(exc).__name__)
            return await self._quotas.tokens_since(scope, subject, since)
        if raw is not None:
            return int(raw)

        used = await self._quotas.tokens_since(scope, subject, since)
        try:
            # NX: if another request seeded it first, keep theirs — both read the same
            # committed rows, and theirs may already include a later charge.
            await self._redis.set(key, used, ex=ttl, nx=True)
            raw = await self._redis.get(key)
        except Exception:
            return used
        return int(raw) if raw is not None else used

    async def _acquire(self, scope: str, subject: str, limit: int) -> _Slot | None:
        key = f"gw:conc:{scope}:{subject}"
        member = uuid.uuid4().hex
        now = time.time()
        lease = self._settings.concurrency_lease_seconds
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.zremrangebyscore(key, "-inf", now - lease)
            pipe.zadd(key, {member: now})
            pipe.zcard(key)
            pipe.expire(key, lease)
            _, _, in_flight, _ = await pipe.execute()
        except Exception as exc:
            log.warning("concurrency_limit_unavailable", error=type(exc).__name__)
            return None

        if int(in_flight) > limit:
            await self._release_slots([_Slot(scope, key, member)])
            self._refuse(
                scope,
                "max_concurrency",
                f"{_describe(scope, subject).capitalize()} already has {limit} "
                "request(s) in flight. Retry when one finishes.",
                limit=limit,
                retry_after=1,
            )
        return _Slot(scope, key, member)

    async def _release_slots(self, slots: Sequence[_Slot]) -> None:
        for slot in slots:
            try:
                await self._redis.zrem(slot.key, slot.member)
            except Exception as exc:
                # The lease lapses on its own; logged so a persistent failure is visible.
                log.warning("concurrency_release_failed", error=type(exc).__name__)

    @staticmethod
    def _refuse(scope: str, limit_name: str, message: str, **details: Any) -> None:
        metrics.GATEWAY_QUOTA_REJECTIONS.labels(scope, limit_name).inc()
        retry_after = details.pop("retry_after", 60)
        raise RateLimitError(
            message,
            details={
                "scope": scope,
                "quota": limit_name,
                "retry_after_seconds": max(1, int(retry_after)),
                **details,
            },
        )


def _keys(scope: str, subject: str, now: dt.datetime) -> dict[str, str]:
    base = f"{scope}:{subject}"
    return {
        "minute": f"gw:tpm:{base}:{int(now.timestamp() // 60)}",
        "day": f"gw:day:{base}:{now:%Y%m%d}",
        "month": f"gw:month:{base}:{now:%Y%m}",
    }


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Circuit:
    failures: deque[float] = field(default_factory=deque)
    open_until: float = 0.0
    half_open: bool = False
    last_error: str | None = None


class CircuitBreakers:
    """Per-backend circuit breakers, held in this control-plane process.

    In-process rather than in Valkey on purpose: a breaker is consulted on every call and
    must not itself depend on a network hop, and each replica discovering a dead backend
    within a handful of failures is good enough. Keyed by ``backend:<id>`` for models
    linked to an inference backend, else ``deployment:<id>`` — so every model on one dead
    server trips together.

    States: closed → (``threshold`` failures within ``window``) → open → (after
    ``open_seconds``) half-open → one success closes it, one failure re-opens it.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._circuits: dict[str, _Circuit] = {}
        self._clock = clock

    def open_for(self, key: str) -> float | None:
        """Seconds until ``key`` may be tried again, or None if it may be tried now."""
        circuit = self._circuits.get(key)
        if circuit is None or not circuit.open_until:
            return None
        remaining = circuit.open_until - self._clock()
        if remaining > 0:
            return remaining
        # Cool-down over: let traffic through, but one more failure re-opens at once.
        circuit.open_until = 0.0
        circuit.half_open = True
        return None

    def record_success(self, key: str) -> None:
        circuit = self._circuits.get(key)
        if circuit is not None and (circuit.failures or circuit.half_open):
            if circuit.half_open:
                log.info("circuit_closed", target=key)
            self._circuits.pop(key, None)

    def record_failure(self, key: str, settings: GatewaySettings, error: str = "") -> bool:
        """Count one upstream failure. Returns True if this failure opened the circuit."""
        now = self._clock()
        circuit = self._circuits.setdefault(key, _Circuit())
        circuit.last_error = error[:200] or None
        circuit.failures.append(now)
        while circuit.failures and circuit.failures[0] < now - settings.circuit_window_seconds:
            circuit.failures.popleft()

        if circuit.open_until > now:
            return False
        if circuit.half_open or len(circuit.failures) >= settings.circuit_failure_threshold:
            circuit.open_until = now + settings.circuit_open_seconds
            circuit.half_open = False
            metrics.GATEWAY_CIRCUIT_OPENS.inc()
            log.warning(
                "circuit_opened",
                target=key,
                failures=len(circuit.failures),
                open_seconds=settings.circuit_open_seconds,
                error=circuit.last_error,
            )
            return True
        return False

    def snapshot(self) -> list[dict[str, Any]]:
        """Every circuit with recent failures, for the admin view."""
        now = self._clock()
        rows = []
        for key, circuit in sorted(self._circuits.items()):
            remaining = circuit.open_until - now if circuit.open_until else 0.0
            state = "open" if remaining > 0 else ("half_open" if circuit.half_open else "closed")
            rows.append(
                {
                    "target": key,
                    "state": state,
                    "recent_failures": len(circuit.failures),
                    "retry_in_seconds": round(max(remaining, 0.0), 1),
                    "last_error": circuit.last_error,
                }
            )
        return rows

    def reset(self) -> None:
        self._circuits.clear()


#: The process-wide breaker table. One per control-plane replica — see the class note.
BREAKERS = CircuitBreakers()
