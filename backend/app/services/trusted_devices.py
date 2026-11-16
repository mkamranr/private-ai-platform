"""Browsers that may skip the two-factor step for a while (expansion E20).

After a successful two-factor step a person may tick "trust this browser". The browser
then keeps a random token and sends it with the password at its next sign-ins; while the
token is good, the code is not asked for.

* Only the token's SHA-256 hash is stored. It is 256 bits of randomness, so a slow hash
  would add nothing (the same reasoning as API keys).
* A token is good only for the account's ``session_version`` at the time it was issued.
  Changing the password, "sign out everywhere", a refresh token being stolen, and an
  administrator's 2FA reset all raise it, so every trusted browser is forgotten at once.
  Turning two-factor off deletes them.
* It skips only the second step. The password is always checked, and a locked account
  stays locked.
* ``auth.mfa_remember_days`` (a runtime setting, 0 = off) sets the lifetime. Setting it to
  0 also stops existing tokens from being honoured.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.audit import AuditAction
from app.models.auth import TrustedDevice, User
from app.services.audit import AuditService

TOKEN_PREFIX = "tdv_"  # noqa: S105 — a visible prefix, not a credential
MAX_PER_USER = 20


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def describe_user_agent(user_agent: str | None) -> str:
    """A short, human label: "Firefox on Windows". Never parsed for anything but display."""
    ua = user_agent or ""
    browser = next(
        (
            name
            for marker, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if marker in ua
        ),
        "A browser",
    )
    system = next(
        (
            name
            for marker, name in (
                ("Windows", "Windows"),
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Mac OS X", "macOS"),
                ("Linux", "Linux"),
            )
            if marker in ua
        ),
        None,
    )
    return f"{browser} on {system}" if system else browser


class TrustedDeviceService:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self._session = session
        self._audit = audit

    async def issue(
        self, user: User, *, days: int, user_agent: str | None
    ) -> tuple[str, dt.datetime]:
        """A new token for this browser, and when it stops working."""
        now = dt.datetime.now(dt.UTC)
        expires = now + dt.timedelta(days=days)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        # Keep the newest MAX_PER_USER - 1, so the table cannot grow without bound.
        old = (
            (
                await self._session.execute(
                    select(TrustedDevice.id)
                    .where(TrustedDevice.user_id == user.id)
                    .order_by(TrustedDevice.created_at.desc())
                    .offset(MAX_PER_USER - 1)
                )
            )
            .scalars()
            .all()
        )
        if old:
            await self._session.execute(delete(TrustedDevice).where(TrustedDevice.id.in_(old)))
        device = TrustedDevice(
            user_id=user.id,
            token_hash=_hash(token),
            session_version=user.session_version or 0,
            label=describe_user_agent(user_agent),
            expires_at=expires,
        )
        self._session.add(device)
        await self._session.flush()
        await self._audit.record(
            AuditAction.USER_DEVICE_TRUSTED,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            user_agent=user_agent,
            metadata={"device": str(device.id), "label": device.label, "days": days},
        )
        return token, expires

    async def accepts(self, user: User, token: str | None) -> TrustedDevice | None:
        """The trusted device ``token`` names, if it is still good for this account."""
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        device = (
            await self._session.execute(
                select(TrustedDevice).where(TrustedDevice.token_hash == _hash(token))
            )
        ).scalar_one_or_none()
        now = dt.datetime.now(dt.UTC)
        if (
            device is None
            or device.user_id != user.id
            or device.expires_at <= now
            or device.session_version != (user.session_version or 0)
        ):
            return None
        device.last_used_at = now
        return device

    async def list_for(self, user: User) -> list[TrustedDevice]:
        """The account's trusted browsers that still work."""
        now = dt.datetime.now(dt.UTC)
        rows = await self._session.execute(
            select(TrustedDevice)
            .where(
                TrustedDevice.user_id == user.id,
                TrustedDevice.expires_at > now,
                TrustedDevice.session_version == (user.session_version or 0),
            )
            .order_by(TrustedDevice.created_at.desc())
        )
        return list(rows.scalars().all())

    async def forget(self, user: User, device_id: uuid.UUID) -> None:
        device = await self._session.get(TrustedDevice, device_id)
        if device is None or device.user_id != user.id:
            raise NotFoundError("No such trusted browser on your account.")
        await self._session.delete(device)
        await self._audit.record(
            AuditAction.USER_DEVICE_FORGOTTEN,
            user_id=user.id,
            username=user.username,
            resource_type="user",
            resource_id=str(user.id),
            metadata={"device": str(device_id), "label": device.label},
        )

    async def forget_all(self, user: User) -> int:
        result = await self._session.execute(
            delete(TrustedDevice).where(TrustedDevice.user_id == user.id)
        )
        count = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if count:
            await self._audit.record(
                AuditAction.USER_DEVICE_FORGOTTEN,
                user_id=user.id,
                username=user.username,
                resource_type="user",
                resource_id=str(user.id),
                metadata={"all": True, "count": count},
            )
        return count
