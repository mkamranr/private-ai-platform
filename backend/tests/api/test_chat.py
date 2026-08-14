"""End-user attribution and the dashboard (M17, M21)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.models_registry import ApiClient, ApiKey, ModelDeployment, UsageRecord
from tests.api.conftest import _user_with
from tests.conftest import auth_header

ISSUER = "open-webui"
#: HS256 keys must be >= 32 bytes (RFC 7518 §3.2); the API enforces it.
SECRET_32 = "a-provisioning-secret-of-at-least-32b"


def mint(secret: str, **overrides: Any) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    claims = {
        "sub": "owui-user-id",
        "email": "ameera@ai-platform.local",
        "name": "Ameera",
        "role": "user",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 300,
        **overrides,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
async def trusted_client(session: AsyncSession, settings) -> tuple[ApiClient, str, str]:
    """A client allowed to assert identity, with a signing secret.

    Returns ``(client, plaintext_key, jwt_secret)`` — the client itself rather than the
    key, because ``ApiKey.client`` is a lazy relationship and the resolver under test is
    synchronous.
    """
    from app.core.security import SecretCipher, generate_api_key

    jwt_secret = "test-signing-secret-at-least-32-bytes-long"
    client = ApiClient(
        name=f"frontend-{uuid.uuid4().hex[:8]}",
        trusted_identity_headers=True,
        identity_jwt_secret_encrypted=SecretCipher(settings.security).encrypt(jwt_secret),
    )
    session.add(client)
    await session.flush()

    plaintext, prefix, key_hash = generate_api_key()
    key = ApiKey(
        client_id=client.id,
        name="k",
        prefix=prefix,
        key_hash=key_hash,
        rate_limit_per_minute=10_000,
    )
    session.add(key)
    await session.flush()
    return client, plaintext, jwt_secret


@pytest.fixture
async def header_only_client(session: AsyncSession) -> tuple[ApiClient, str]:
    """Trusted, but with no signing secret — the plaintext-header path."""
    from app.core.security import generate_api_key

    client = ApiClient(name=f"plain-{uuid.uuid4().hex[:8]}", trusted_identity_headers=True)
    session.add(client)
    await session.flush()

    plaintext, prefix, key_hash = generate_api_key()
    key = ApiKey(
        client_id=client.id,
        name="k",
        prefix=prefix,
        key_hash=key_hash,
        rate_limit_per_minute=10_000,
    )
    session.add(key)
    await session.flush()
    return client, plaintext


async def _chat(client: AsyncClient, secret: str, model: str, **headers: str) -> Any:
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {secret}", **headers},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


async def _last_record(database, key_id: uuid.UUID) -> UsageRecord | None:
    """Read from an independent session — usage is written on one too."""
    async with database.sessionmaker() as verify:
        return (
            await verify.execute(
                select(UsageRecord)
                .where(UsageRecord.api_key_id == key_id)
                .order_by(UsageRecord.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Attribution (M17)
# ---------------------------------------------------------------------------
class TestForwardedIdentity:
    async def test_signed_identity_is_attributed(
        self,
        gateway_app,
        client: AsyncClient,
        committed_gateway_trusted: dict[str, Any],
        database,
    ) -> None:
        """The M17 deliverable: one service key, per-person accounting."""
        response = await _chat(
            client,
            committed_gateway_trusted["secret"],
            committed_gateway_trusted["model_name"],
            **{"X-OpenWebUI-User-Jwt": mint(committed_gateway_trusted["jwt_secret"])},
        )
        assert response.status_code == 200

        record = await _last_record(database, committed_gateway_trusted["key_id"])
        assert record is not None
        assert record.end_user == "ameera@ai-platform.local"
        assert record.end_user_trusted is True

    async def test_untrusted_client_cannot_assert_identity(
        self,
        gateway_app,
        client: AsyncClient,
        committed_gateway: dict[str, Any],
        database,
    ) -> None:
        """**The whole point of the flag.** A forwarded identity is an unauthenticated
        string; honouring it from any key would let any developer bill their traffic to
        someone else."""
        response = await _chat(
            client,
            committed_gateway["secret"],
            committed_gateway["model_name"],
            **{
                "X-OpenWebUI-User-Email": "ceo@ai-platform.local",
                "X-OpenWebUI-User-Id": "not-mine",
            },
        )
        assert response.status_code == 200

        record = await _last_record(database, committed_gateway["key_id"])
        assert record is not None
        assert record.end_user is None, "an untrusted client asserted an identity"

    async def test_signed_client_ignores_plaintext_headers(
        self, gateway_app, client: AsyncClient, trusted_client, serving_deployment
    ) -> None:
        """No fallback once a secret exists. Falling back would hand an attacker a way
        around the signature: simply omit the signed header."""
        api_client, secret, _ = trusted_client
        response = await _chat(
            client,
            secret,
            serving_deployment.model.name,
            **{"X-OpenWebUI-User-Email": "ceo@ai-platform.local"},
        )
        assert response.status_code == 200
        # Verified through the resolver rather than the record: this fixture lives in the
        # test transaction, which the usage writer's own session cannot see.
        from app.services.identity import resolve_forwarded_identity

        assert (
            resolve_forwarded_identity(
                _headers({"X-OpenWebUI-User-Email": "ceo@ai-platform.local"}),
                api_client,
                _gateway_settings(),
                _cipher(),
            )
            is None
        )

    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("expired", {"exp": int(dt.datetime.now(dt.UTC).timestamp()) - 60}),
            ("wrong issuer", {"iss": "somebody-else"}),
        ],
    )
    async def test_invalid_assertions_are_refused(
        self, trusted_client, label: str, overrides: dict[str, Any]
    ) -> None:
        from app.services.identity import resolve_forwarded_identity

        api_client, _, jwt_secret = trusted_client
        token = mint(jwt_secret, **overrides)
        assert (
            resolve_forwarded_identity(
                _headers({"X-OpenWebUI-User-Jwt": token}),
                api_client,
                _gateway_settings(),
                _cipher(),
            )
            is None
        ), label

    async def test_forged_signature_is_refused(self, trusted_client) -> None:
        from app.services.identity import resolve_forwarded_identity

        api_client, _, _ = trusted_client
        assert (
            resolve_forwarded_identity(
                _headers({"X-OpenWebUI-User-Jwt": mint("a-different-secret-also-32-bytes-long")}),
                api_client,
                _gateway_settings(),
                _cipher(),
            )
            is None
        )

    async def test_plaintext_accepted_when_no_secret_configured(self, header_only_client) -> None:
        """Supported because not every frontend can sign — weaker, and documented as such."""
        from app.services.identity import resolve_forwarded_identity

        api_client, _ = header_only_client
        resolved = resolve_forwarded_identity(
            _headers({"X-OpenWebUI-User-Email": "ameera@ai-platform.local"}),
            api_client,
            _gateway_settings(),
            _cipher(),
        )
        assert resolved is not None
        assert resolved.subject == "ameera@ai-platform.local"
        assert resolved.trusted is True

    async def test_self_reported_user_field_is_recorded_untrusted(
        self,
        gateway_app,
        client: AsyncClient,
        committed_gateway: dict[str, Any],
        database,
    ) -> None:
        """OpenAI's `user` field. Worth keeping for per-tenant breakdown, never billable."""
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={
                "model": committed_gateway["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
                "user": "tenant-42",
            },
        )
        assert response.status_code == 200

        record = await _last_record(database, committed_gateway["key_id"])
        assert record is not None
        assert record.end_user == "tenant-42"
        assert record.end_user_trusted is False

    async def test_signed_identity_wins_over_the_user_field(
        self,
        gateway_app,
        client: AsyncClient,
        committed_gateway_trusted: dict[str, Any],
        database,
    ) -> None:
        response = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {committed_gateway_trusted['secret']}",
                "X-OpenWebUI-User-Jwt": mint(committed_gateway_trusted["jwt_secret"]),
            },
            json={
                "model": committed_gateway_trusted["model_name"],
                "messages": [{"role": "user", "content": "hi"}],
                "user": "tenant-42",
            },
        )
        assert response.status_code == 200

        record = await _last_record(database, committed_gateway_trusted["key_id"])
        assert record is not None
        assert record.end_user == "ameera@ai-platform.local"
        assert record.end_user_trusted is True

    async def test_client_creation_never_returns_the_secret(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        admin = await _user_with(
            session, settings, [Perm.APIKEY_MANAGE, Perm.APIKEY_VIEW], name="keyadmin"
        )
        response = await client.post(
            "/api/v1/api-clients",
            headers=auth_header(tokens, admin),
            json={
                "name": f"ui-{uuid.uuid4().hex[:6]}",
                "trusted_identity_headers": True,
                "identity_jwt_secret": SECRET_32,
            },
        )
        assert response.status_code == 201
        assert SECRET_32 not in response.text
        assert response.json()["identity_signature_required"] is True

    async def test_secret_without_the_flag_is_rejected(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """Verifying a signature and then discarding the identity is a silent no-op —
        exactly the shape of misconfiguration nobody notices."""
        admin = await _user_with(session, settings, [Perm.APIKEY_MANAGE], name="keyadmin2")
        response = await client.post(
            "/api/v1/api-clients",
            headers=auth_header(tokens, admin),
            json={
                "name": f"ui-{uuid.uuid4().hex[:6]}",
                "identity_jwt_secret": SECRET_32,
            },
        )
        assert response.status_code == 422

    async def test_usage_by_user_separates_trusted_from_self_reported(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """A chargeback report that added them together would bill someone for traffic
        anyone could have labelled as theirs."""
        viewer = await _user_with(session, settings, [Perm.USAGE_VIEW], name="usage")
        shared = f"shared-{uuid.uuid4().hex[:6]}@x.local"
        for trusted in (True, False):
            session.add(
                UsageRecord(
                    endpoint="chat.completions",
                    requested_model="m",
                    model="m",
                    prompt_tokens=10,
                    completion_tokens=20,
                    end_user=shared,
                    end_user_trusted=trusted,
                )
            )
        await session.flush()

        rows = (
            await client.get("/api/v1/usage/by-user", headers=auth_header(tokens, viewer))
        ).json()["rows"]
        mine = [r for r in rows if r["end_user"] == shared]
        assert len(mine) == 2, "trusted and self-reported usage were merged into one row"
        assert {r["trusted"] for r in mine} == {True, False}


# ---------------------------------------------------------------------------
# Dashboard (M21)
# ---------------------------------------------------------------------------
class TestDashboard:
    async def test_requires_monitoring_view(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        nobody = await _user_with(session, settings, [], name="nobody")
        response = await client.get("/api/v1/dashboard", headers=auth_header(tokens, nobody))
        assert response.status_code == 403

    async def test_superuser_sees_every_section(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        body = (
            await client.get("/api/v1/dashboard", headers=auth_header(tokens, superuser))
        ).json()
        assert set(body) >= {"fleet", "gpus", "models", "gateway", "activity"}
        assert body["window_hours"] == 24

    async def test_sections_are_omitted_not_blanked(
        self, client: AsyncClient, tokens, session: AsyncSession, settings
    ) -> None:
        """A zero is a claim about the platform. "You cannot see this" is a different
        claim, and a dashboard that renders them identically is lying to the operator."""
        infra = await _user_with(
            session,
            settings,
            [Perm.MONITORING_VIEW, Perm.INFRASTRUCTURE_VIEW, Perm.GPU_VIEW],
            name="infra",
        )
        body = (await client.get("/api/v1/dashboard", headers=auth_header(tokens, infra))).json()
        assert body["fleet"] is not None
        assert body["gpus"] is not None
        assert body["activity"] is None, "audit trail leaked to a user without audit.view"
        assert body["gateway"] is None, "usage leaked to a user without usage.view"

    async def test_synthetic_nodes_are_counted(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        """Presenting fabricated capacity as real is the worst thing this screen can do,
        so the count is part of the contract, not a UI detail."""
        from app.models.infrastructure import Node, NodeStatus

        session.add(
            Node(
                name=f"fake-{uuid.uuid4().hex[:8]}",
                agent_url="http://a:9100",
                agent_token_encrypted="x",
                status=NodeStatus.ONLINE,
                gpu_synthetic=True,
            )
        )
        await session.flush()

        fleet = (
            await client.get("/api/v1/dashboard", headers=auth_header(tokens, superuser))
        ).json()["fleet"]
        assert fleet["synthetic"] >= 1

    async def test_deployment_counts(
        self, client: AsyncClient, tokens, superuser: User, serving_deployment: ModelDeployment
    ) -> None:
        models = (
            await client.get("/api/v1/dashboard", headers=auth_header(tokens, superuser))
        ).json()["models"]
        assert models["running"] >= 1
        assert models["registered"] >= 1

    async def test_window_is_bounded(self, client: AsyncClient, tokens, superuser: User) -> None:
        assert (
            await client.get(
                "/api/v1/dashboard?window_hours=99999", headers=auth_header(tokens, superuser)
            )
        ).status_code == 422


# ---------------------------------------------------------------------------
# Catalogue (§13)
# ---------------------------------------------------------------------------
class TestCatalogueDoesNotLeakAliases:
    async def test_alias_target_is_not_published(
        self,
        gateway_app,
        client: AsyncClient,
        session: AsyncSession,
        api_key_pair: tuple[ApiKey, str],
        serving_deployment: ModelDeployment,
    ) -> None:
        """Stripping the model from completion responses and then publishing the mapping
        in the catalogue would defeat the point — somebody will branch on what they can
        see, and repointing the alias then breaks them."""
        from app.models.models_registry import ModelAlias

        alias = ModelAlias(
            alias=f"ent-{uuid.uuid4().hex[:6]}", model_id=serving_deployment.model_id
        )
        session.add(alias)
        await session.flush()

        _, secret = api_key_pair
        body = (
            await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        ).json()
        entry = next(m for m in body["data"] if m["id"] == alias.alias)
        assert "aliased_model" not in entry
        assert serving_deployment.model.name not in str(entry)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _headers(values: dict[str, str]):
    from starlette.datastructures import Headers

    return Headers(values)


def _gateway_settings():
    from app.config.settings import get_settings

    return get_settings().gateway


def _cipher():
    from app.config.settings import get_settings
    from app.core.security import SecretCipher

    return SecretCipher(get_settings().security)
