"""API clients, keys and usage reporting (M20, minimal in Phase 2).

The gateway is unusable without a credential, and §20's MVP scenario is explicit that
"Developer creates API key" precedes calling it — so the minimum viable slice lands here
rather than waiting for Phase 6. The developer portal UI, quotas and per-scope
permissions remain Phase 6.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.security import SecretCipher, generate_api_key
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import ApiClient, ApiKey
from app.repositories.models_registry import (
    ApiClientRepository,
    ApiKeyRepository,
    UsageRepository,
)
from app.services.audit import AuditService

log = get_logger(__name__)


class ApiKeyService:
    def __init__(
        self,
        clients: ApiClientRepository,
        keys: ApiKeyRepository,
        usage: UsageRepository,
        audit: AuditService,
        cipher: SecretCipher,
    ) -> None:
        self._clients = clients
        self._keys = keys
        self._usage = usage
        self._audit = audit
        self._cipher = cipher

    async def list_clients(self) -> list[ApiClient]:
        return list(await self._clients.list_all())

    async def create_client(
        self,
        *,
        name: str,
        description: str | None,
        actor: User,
        trusted_identity_headers: bool = False,
        identity_jwt_secret: str | None = None,
    ) -> ApiClient:
        """Register an application.

        ``trusted_identity_headers`` grants this client the right to tell the gateway who
        a request is *for* (M17). Off unless asked for: it belongs to frontends the
        platform deploys and whose users it authenticates, and granting it to an ordinary
        developer application would let that application bill its usage to anyone.

        ``identity_jwt_secret`` upgrades that from a flag to a signature — with one set,
        only a validly signed assertion is accepted, so holding the API key is no longer
        enough to forge an identity. Stored Fernet-encrypted.
        """
        if await self._clients.get_by_name(name):
            raise ConflictError(f"An API client named {name!r} already exists.")
        if identity_jwt_secret and not trusted_identity_headers:
            raise ValidationError(
                "identity_jwt_secret is meaningless without trusted_identity_headers: "
                "the signature would be verified and then the identity discarded."
            )

        client = ApiClient(
            name=name,
            description=description,
            owner_id=actor.id,
            trusted_identity_headers=trusted_identity_headers,
            identity_jwt_secret_encrypted=(
                self._cipher.encrypt(identity_jwt_secret) if identity_jwt_secret else None
            ),
        )
        self._clients.add(client)
        await self._clients.flush()

        await self._audit.record(
            AuditAction.API_CLIENT_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="api_client",
            resource_id=str(client.id),
            metadata={
                "name": name,
                "trusted_identity_headers": trusted_identity_headers,
                # Whether, not what.
                "identity_signature_required": bool(identity_jwt_secret),
            },
        )
        return client

    async def list_keys(self) -> list[ApiKey]:
        return list(await self._keys.list_all())

    async def create_key(
        self,
        *,
        client_id: uuid.UUID,
        name: str,
        rate_limit_per_minute: int,
        expires_at: dt.datetime | None,
        actor: User,
        scopes: list[str] | None = None,
    ) -> tuple[ApiKey, str]:
        """Mint a key. Returns ``(record, plaintext)``.

        The plaintext is returned **once** and never stored. Only its SHA-256 hash and a
        short prefix are persisted, so a database read yields nothing usable — which is
        the whole point, and the reason the API cannot show a key again later.
        """
        client = await self._clients.get(client_id)
        if client is None:
            raise NotFoundError(f"No API client with id {client_id}.")
        if expires_at is not None and expires_at <= dt.datetime.now(dt.UTC):
            raise ValidationError("expires_at must be in the future.")

        full_key, prefix, key_hash = generate_api_key()
        key = ApiKey(
            client_id=client.id,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            rate_limit_per_minute=rate_limit_per_minute,
            scopes=list(scopes or []),
            expires_at=expires_at,
            created_by=actor.id,
        )
        self._keys.add(key)
        await self._keys.flush()

        await self._audit.record(
            AuditAction.API_KEY_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="api_key",
            resource_id=str(key.id),
            # Prefix only. Recording the key itself would put a live credential in the
            # most widely readable table in the platform.
            metadata={
                "client": client.name,
                "name": name,
                "prefix": prefix,
                "scopes": list(scopes or []) or ["(unrestricted)"],
            },
        )
        return key, full_key

    async def rotate_key(
        self, key_id: uuid.UUID, *, grace_hours: int = 24, actor: User
    ) -> tuple[ApiKey, str, ApiKey]:
        """Mint a replacement and put the old key on a timer.

        Returns ``(new_key, plaintext, old_key)``.

        The old key is **not** revoked immediately. A credential rotated with no overlap
        takes down every integration still holding it at the instant of rotation, which is
        why rotation gets skipped and keys live for years. A grace window means an operator
        can rotate first and redeploy afterwards.

        The old key's expiry only ever moves *earlier*: rotating a key that already expires
        sooner than the grace window must not extend its life.
        """
        old = await self._keys.get(key_id)
        if old is None:
            raise NotFoundError(f"No API key with id {key_id}.")
        if old.revoked_at is not None:
            raise ValidationError(
                "That key is already revoked. Rotation replaces a live key; create a new "
                "one instead."
            )
        if grace_hours < 0:
            raise ValidationError("grace_hours cannot be negative.")

        new_key, plaintext = await self.create_key(
            client_id=old.client_id,
            name=f"{old.name} (rotated)",
            rate_limit_per_minute=old.rate_limit_per_minute,
            expires_at=old.expires_at,
            scopes=list(old.scopes or []),
            actor=actor,
        )

        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(hours=grace_hours)
        old.expires_at = min(old.expires_at, deadline) if old.expires_at else deadline
        old.rotated_to = new_key.id

        await self._audit.record(
            AuditAction.API_KEY_REVOKED,
            user_id=actor.id,
            username=actor.username,
            resource_type="api_key",
            resource_id=str(old.id),
            metadata={
                "rotated_to": str(new_key.id),
                "old_prefix": old.prefix,
                "new_prefix": new_key.prefix,
                "old_key_expires_at": old.expires_at.isoformat(),
                "grace_hours": grace_hours,
            },
        )
        return new_key, plaintext, old

    async def revoke_key(self, key_id: uuid.UUID, *, actor: User) -> ApiKey:
        key = await self._keys.get(key_id)
        if key is None:
            raise NotFoundError(f"No API key with id {key_id}.")
        if key.revoked_at is None:
            key.revoked_at = dt.datetime.now(dt.UTC)

        await self._audit.record(
            AuditAction.API_KEY_REVOKED,
            user_id=actor.id,
            username=actor.username,
            resource_type="api_key",
            resource_id=str(key.id),
            metadata={"prefix": key.prefix},
        )
        return key

    async def usage_summary(self, *, since: dt.datetime) -> list[dict]:
        return await self._usage.summary(since=since)

    async def usage_by_end_user(self, *, since: dt.datetime, limit: int = 100) -> list[dict]:
        return await self._usage.by_end_user(since=since, limit=limit)
