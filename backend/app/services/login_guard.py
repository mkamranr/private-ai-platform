"""Brute-force defences for sign-in and the ops runner token (expansion E14).

Two independent limits, because they stop different attacks:

* **Per account** (Postgres): ``auth.lockout_threshold`` consecutive wrong passwords lock
  a local account for ``auth.lockout_minutes``. This stops a slow, distributed guess at one
  password. While locked, sign-in is refused with the same message as a wrong password
  and the password is not checked, so the lock cannot be used to learn whether a guess was
  right. An administrator can unlock early.
* **Per client address** (Valkey): ``auth.login_ip_max_failures`` failures in
  ``auth.login_ip_window_seconds`` refuse that address for the rest of the window,
  whichever accounts it tried. This stops password spraying (one guess against many
  accounts), which a per-account lock never sees, and it covers directory (LDAP) accounts,
  which the platform cannot lock. The ops runner endpoints get the same treatment.

**Why the writes have their own transaction.** A failed sign-in ends in an error, and the
request's transaction rolls back. Counting the failure inside it would count nothing,
which is what happened before E14. Counters, locks, alerts and the audit trail of
failures are therefore committed separately.

**Valkey down.** The per-address limit fails open (logged), like the gateway's quotas:
an outage of the cache must not lock everyone out of the console. The per-account lock is
in Postgres and keeps working.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import case, false, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import AuthSettings, OpsSettings
from app.core.errors import RateLimitError
from app.core.logging import get_logger
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.models.operations import AlertSeverity
from app.repositories.audit import AuditRepository
from app.repositories.operations import AlertRepository
from app.services.alerts import AlertService
from app.services.audit import AuditService

log = get_logger(__name__)

AUTH_ALERT_SOURCE = "auth"
RUNNER_ALERT_SOURCE = "ops_runner"


def account_locked_fingerprint(user_id: uuid.UUID | str) -> str:
    return f"account_locked:{user_id}"


def login_throttled_fingerprint(address: str) -> str:
    return f"login_throttled:{address}"


def token_reuse_fingerprint(user_id: uuid.UUID | str) -> str:
    return f"token_reuse:{user_id}"


def runner_throttled_fingerprint(address: str) -> str:
    return f"ops_runner_auth:{address}"


def _minutes(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class FailureThrottle:
    """Failures per key in a fixed window, counted in Valkey. Fails open."""

    def __init__(self, redis: Any, *, prefix: str, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._prefix = prefix
        self.limit = limit
        self.window_seconds = window_seconds

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self._redis is not None

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def retry_after(self, key: str) -> int | None:
        """Seconds until ``key`` may try again, or None if it is not blocked."""
        if not self.enabled:
            return None
        try:
            count = await self._redis.get(self._key(key))
            if count is None or int(count) < self.limit:
                return None
            ttl = await self._redis.ttl(self._key(key))
        except Exception as exc:  # Valkey down: never lock everyone out
            log.warning(
                "failure_throttle_unavailable", prefix=self._prefix, exc_type=type(exc).__name__
            )
            return None
        return int(ttl) if ttl and ttl > 0 else self.window_seconds

    async def fail(self, key: str) -> int:
        """Count one failure. Returns the count in this window (0 if not counted)."""
        if not self.enabled:
            return 0
        try:
            pipe = self._redis.pipeline(transaction=True)
            pipe.incr(self._key(key))
            pipe.expire(self._key(key), self.window_seconds, nx=True)
            count, _ = await pipe.execute()
            return int(count)
        except Exception as exc:
            log.warning(
                "failure_throttle_unavailable", prefix=self._prefix, exc_type=type(exc).__name__
            )
            return 0

    async def clear(self, key: str) -> None:
        if not self.enabled:
            return
        try:
            await self._redis.delete(self._key(key))
        except Exception as exc:
            log.warning(
                "failure_throttle_unavailable", prefix=self._prefix, exc_type=type(exc).__name__
            )


class _Independent:
    """Writes committed in their own transaction, because the request will roll back."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._factory = session_factory

    async def alert(self, **fields: Any) -> None:
        if self._factory is None:
            return
        try:
            async with self._factory() as session:
                audit = AuditService(AuditRepository(session))
                await AlertService(AlertRepository(session), audit).raise_alert(**fields)
                await session.commit()
        except Exception:
            log.exception("independent_alert_failed", fingerprint=fields.get("fingerprint"))

    async def clear_alert(self, fingerprint: str) -> None:
        if self._factory is None:
            return
        try:
            async with self._factory() as session:
                audit = AuditService(AuditRepository(session))
                await AlertService(AlertRepository(session), audit).clear(fingerprint)
                await session.commit()
        except Exception:
            log.exception("independent_alert_failed", fingerprint=fingerprint)


class LoginGuard(_Independent):
    """Lockout, per-address throttling and token-theft response for sign-in."""

    def __init__(
        self,
        settings: AuthSettings,
        redis: Any,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        super().__init__(session_factory)
        self.settings = settings
        self.addresses = FailureThrottle(
            redis,
            prefix="auth:login-fail",
            limit=settings.login_ip_max_failures,
            window_seconds=settings.login_ip_window_seconds,
        )

    async def check_address(self, address: str) -> int | None:
        return await self.addresses.retry_after(address)

    async def address_failed(self, address: str, *, audit: AuditService) -> None:
        count = await self.addresses.fail(address)
        if count and count == self.addresses.limit:
            window = _minutes(self.addresses.window_seconds)
            await audit.record_independent(
                AuditAction.LOGIN_THROTTLED,
                result=AuditResult.DENIED,
                message=f"{count} failed sign-ins from {address}; refused for {window}",
                metadata={"address": address, "failures": count},
            )
            await self.alert(
                fingerprint=login_throttled_fingerprint(address),
                severity=AlertSeverity.WARNING,
                source=AUTH_ALERT_SOURCE,
                title=f"Repeated failed sign-ins from {address}",
                message=(
                    f"{count} failed sign-ins within {window}, possibly password guessing or "
                    f"spraying. The address is refused until the window ends."
                ),
                resource_type="client_address",
                resource_id=address[:64],
            )

    async def account_failed(self, user: User, *, now: dt.datetime) -> tuple[int, bool]:
        """Count a wrong password atomically. Returns (failures so far, locked now)."""
        threshold = self.settings.lockout_threshold
        if self._factory is None:
            return user.failed_login_count + 1, False
        until = now + dt.timedelta(minutes=self.settings.lockout_minutes)
        reaches = (User.failed_login_count + 1 >= threshold) if threshold > 0 else false()
        stmt = (
            update(User)
            .where(User.id == user.id)
            .values(
                failed_login_count=case((reaches, 0), else_=User.failed_login_count + 1),
                locked_until=case((reaches, until), else_=User.locked_until),
            )
            .returning(User.failed_login_count, User.locked_until)
            .execution_options(synchronize_session=False)
        )
        try:
            async with self._factory() as session:
                row = (await session.execute(stmt)).first()
                await session.commit()
        except Exception:
            log.exception("failed_login_count_not_recorded", user_id=str(user.id))
            return user.failed_login_count + 1, False
        if row is None:  # the row is not visible to a new transaction (tests)
            return user.failed_login_count + 1, False
        count, _locked_until = row
        # Locking resets the counter, so a fresh lock is the only way to read back 0.
        locked_now = threshold > 0 and int(count) == 0
        return (threshold if locked_now else int(count)), locked_now

    async def account_locked(self, user: User, *, audit: AuditService, failures: int) -> None:
        minutes = self.settings.lockout_minutes
        await audit.record_independent(
            AuditAction.USER_LOCKED,
            result=AuditResult.DENIED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            message=f"Locked for {minutes} min after {failures} wrong passwords",
            metadata={"failures": failures, "minutes": minutes},
        )
        await self.alert(
            fingerprint=account_locked_fingerprint(user.id),
            severity=AlertSeverity.WARNING,
            source=AUTH_ALERT_SOURCE,
            title=f"Account {user.username} locked after {failures} wrong passwords",
            message=(
                f"Sign-in is refused for {minutes} minutes. If this was the account holder, "
                "unlock it on the Users page; if not, someone may be guessing its password."
            ),
            resource_type="user",
            resource_id=str(user.id),
        )

    async def sign_out_everywhere(self, user: User, *, audit: AuditService, reason: str) -> None:
        """Raise the session version in its own transaction: the request will fail."""
        if self._factory is not None:
            try:
                async with self._factory() as session:
                    await session.execute(
                        update(User)
                        .where(User.id == user.id)
                        .values(session_version=User.session_version + 1)
                        .execution_options(synchronize_session=False)
                    )
                    await session.commit()
            except Exception:
                log.exception("sign_out_everywhere_failed", user_id=str(user.id))
        await audit.record_independent(
            AuditAction.USER_TOKEN_REUSE_DETECTED,
            result=AuditResult.DENIED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            message=reason,
        )
        await self.alert(
            fingerprint=token_reuse_fingerprint(user.id),
            severity=AlertSeverity.CRITICAL,
            source=AUTH_ALERT_SOURCE,
            title=f"Refresh token reused for {user.username}: all sessions signed out",
            message=(
                "A refresh token that had already been exchanged was presented again. That "
                "happens when a token is copied and used by someone else. Every session for "
                "the account has been signed out. Check the audit log for the addresses involved."
            ),
            resource_type="user",
            resource_id=str(user.id),
        )


class RunnerGuard(_Independent):
    """Per-address limit on wrong ops runner tokens (threat model item 12)."""

    def __init__(
        self,
        settings: OpsSettings,
        redis: Any,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        super().__init__(session_factory)
        self.addresses = FailureThrottle(
            redis,
            prefix="ops:runner-fail",
            limit=settings.runner_max_failures,
            window_seconds=settings.runner_failure_window_seconds,
        )

    async def check(self, address: str) -> None:
        retry = await self.addresses.retry_after(address)
        if retry:
            raise RateLimitError(
                f"Too many wrong runner tokens from this address. Try again in {_minutes(retry)}.",
                details={"retry_after_seconds": retry},
            )

    async def failed(self, address: str, *, audit: AuditService) -> None:
        count = await self.addresses.fail(address)
        await audit.record_independent(
            AuditAction.OPS_RUNNER_AUTH_FAILED,
            result=AuditResult.DENIED,
            username="ops-runner",
            message=f"Wrong ops runner token from {address}",
            metadata={"address": address, "failures": count},
        )
        if count and count == self.addresses.limit:
            await self.alert(
                fingerprint=runner_throttled_fingerprint(address),
                severity=AlertSeverity.WARNING,
                source=RUNNER_ALERT_SOURCE,
                title=f"Repeated wrong ops runner tokens from {address}",
                message=(
                    f"{count} wrong tokens within {_minutes(self.addresses.window_seconds)}. "
                    "The ops runner can restore databases and run upgrades, so this address "
                    "is refused until the window ends. Check what is calling the runner API."
                ),
                resource_type="client_address",
                resource_id=address[:64],
            )
