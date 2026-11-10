"""Teams and resource scope (E3): team management, isolation, gateway scope, team quotas."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.core.scope import Scope
from app.core.security import generate_api_key
from app.models.agents import Agent
from app.models.audit import AuditLog
from app.models.auth import User
from app.models.knowledge import KnowledgeBase
from app.models.models_registry import (
    ApiClient,
    ApiKey,
    DeploymentState,
    Model,
    ModelAlias,
    ModelDeployment,
    ModelStatus,
    UsageRecord,
)
from app.models.operations import GatewayQuota
from app.models.teams import Team, TeamMembership
from tests.api.conftest import _user_with
from tests.conftest import auth_header

MESSAGES = [{"role": "user", "content": "hi"}]

EVERYTHING = [
    Perm.MODEL_VIEW,
    Perm.MODEL_REGISTER,
    Perm.MODEL_DEPLOY,
    Perm.MODEL_STOP,
    Perm.MODEL_DELETE,
    Perm.APIKEY_VIEW,
    Perm.APIKEY_MANAGE,
    Perm.USAGE_VIEW,
    Perm.AGENT_VIEW,
    Perm.AGENT_EDIT,
    Perm.AGENT_DELETE,
    Perm.KNOWLEDGE_VIEW,
    Perm.KNOWLEDGE_MANAGE,
    Perm.TEAM_VIEW,
]


def _suffix() -> str:
    return uuid.uuid4().hex[:8]


async def _team(session: AsyncSession, label: str) -> Team:
    suffix = _suffix()
    team = Team(name=f"Team {label} {suffix}", slug=f"team-{label.lower()}-{suffix}")
    session.add(team)
    await session.flush()
    return team


async def _member(session: AsyncSession, team: Team, user: User, role: str = "member") -> None:
    session.add(TeamMembership(team_id=team.id, user_id=user.id, role=role))
    await session.flush()


async def _model(session: AsyncSession, team: Team | None, *, serving: bool = False) -> Model:
    model = Model(
        name=f"scoped-{_suffix()}",
        display_name="Scoped",
        type="LLM",
        storage_path="/x",
        runtime="mock",
        status=ModelStatus.AVAILABLE,
        team_id=team.id if team else None,
    )
    session.add(model)
    await session.flush()
    if serving:
        session.add(
            ModelDeployment(
                model_id=model.id,
                node_id=None,
                state=DeploymentState.RUNNING,
                runtime="mock",
                image="",
                internal_port=8000,
                internal_url=f"http://{model.name}:8000",
                gpu_indices=[],
            )
        )
        await session.flush()
    return model


async def _alias(session: AsyncSession, model: Model, team: Team | None) -> ModelAlias:
    alias = ModelAlias(
        alias=f"alias-{_suffix()}", model_id=model.id, team_id=team.id if team else None
    )
    session.add(alias)
    await session.flush()
    return alias


async def _key(session: AsyncSession, team: Team | None) -> tuple[ApiKey, str]:
    client = ApiClient(name=f"client-{_suffix()}", team_id=team.id if team else None)
    session.add(client)
    await session.flush()
    secret, prefix, key_hash = generate_api_key()
    key = ApiKey(
        client_id=client.id,
        name="scoped",
        prefix=prefix,
        key_hash=key_hash,
        rate_limit_per_minute=10_000,
    )
    session.add(key)
    await session.flush()
    return key, secret


@pytest.fixture
async def world(session: AsyncSession, settings) -> dict[str, Any]:
    """Two teams, a member of A, and one of each resource: shared, in A and in B."""
    a, b = await _team(session, "A"), await _team(session, "B")
    alice = await _user_with(session, settings, EVERYTHING, name="alice")
    await _member(session, a, alice)

    owners = (("shared", None), ("a", a), ("b", b))
    models = {k: await _model(session, t, serving=True) for k, t in owners}
    aliases = {k: await _alias(session, models[k], t) for k, t in owners}
    keys = {k: await _key(session, t) for k, t in owners}
    agents = {}
    bases = {}
    for k, t in owners:
        agent = Agent(slug=f"agent-{k}-{_suffix()}", display_name=k, team_id=t.id if t else None)
        base = KnowledgeBase(
            name=f"kb-{k}-{_suffix()}",
            display_name=k,
            collection=f"kb_{_suffix()}",
            embedding_model="emb",
            embedding_dimensions=8,
            team_id=t.id if t else None,
        )
        session.add_all([agent, base])
        agents[k], bases[k] = agent, base
    await session.flush()
    return {
        "a": a,
        "b": b,
        "alice": alice,
        "models": models,
        "aliases": aliases,
        "keys": keys,
        "agents": agents,
        "bases": bases,
    }


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------
class TestScopeRule:
    def test_shared_is_visible_to_everyone_and_teams_only_to_members(self) -> None:
        t1, t2 = uuid.uuid4(), uuid.uuid4()
        member = Scope(team_ids=frozenset({t1}))
        assert member.can_see(None) and member.can_see(t1) and not member.can_see(t2)
        assert Scope.everything().can_see(t2)
        assert Scope.for_team(None).can_see(None) and not Scope.for_team(None).can_see(t1)

    def test_clause_is_omitted_for_all_teams(self) -> None:
        assert Scope.everything().clause(Model.team_id) is None
        assert Scope().clause(Model.team_id) is not None


# ---------------------------------------------------------------------------
# Team management
# ---------------------------------------------------------------------------
class TestTeamManagement:
    async def test_create_list_rename_and_delete(
        self, client: AsyncClient, tokens, superuser: User, session: AsyncSession
    ) -> None:
        headers = auth_header(tokens, superuser)
        name = f"Platform {_suffix()}"
        created = await client.post("/api/v1/teams", headers=headers, json={"name": name})
        assert created.status_code == 201, created.text
        team = created.json()
        assert team["slug"] == name.lower().replace(" ", "-")

        listed = await client.get("/api/v1/teams", headers=headers)
        assert team["id"] in {t["id"] for t in listed.json()}

        renamed = await client.patch(
            f"/api/v1/teams/{team['id']}", headers=headers, json={"description": "ops"}
        )
        assert renamed.status_code == 200 and renamed.json()["description"] == "ops"

        duplicate = await client.post("/api/v1/teams", headers=headers, json={"name": name})
        assert duplicate.status_code == 409

        deleted = await client.delete(f"/api/v1/teams/{team['id']}", headers=headers)
        assert deleted.status_code == 200
        actions = (
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == team["id"])
                )
            )
            .scalars()
            .all()
        )
        assert {"TEAM_CREATED", "TEAM_UPDATED", "TEAM_DELETED"} <= set(actions)

    async def test_a_team_that_owns_resources_cannot_be_deleted(
        self, client: AsyncClient, tokens, superuser: User, world: dict[str, Any]
    ) -> None:
        response = await client.delete(
            f"/api/v1/teams/{world['b'].id}", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["resources"]["models"] == 1

    async def test_members_see_only_their_own_teams(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])
        ids = {t["id"] for t in (await client.get("/api/v1/teams", headers=headers)).json()}
        assert str(world["a"].id) in ids and str(world["b"].id) not in ids
        assert (
            await client.get(f"/api/v1/teams/{world['b'].id}", headers=headers)
        ).status_code == 404

        mine = (await client.get("/api/v1/teams/mine", headers=headers)).json()
        assert mine["all_teams"] is False
        assert [t["id"] for t in mine["teams"]] == [str(world["a"].id)]

    async def test_owners_manage_members_and_members_do_not(
        self, client: AsyncClient, tokens, session: AsyncSession, settings, world: dict[str, Any]
    ) -> None:
        owner = await _user_with(session, settings, [Perm.TEAM_VIEW], name="owner")
        await _member(session, world["a"], owner, role="owner")
        newcomer = await _user_with(session, settings, [], name="newcomer")
        path = f"/api/v1/teams/{world['a'].id}/members"

        refused = await client.post(
            path, headers=auth_header(tokens, world["alice"]), json={"user_id": str(newcomer.id)}
        )
        assert refused.status_code == 403

        added = await client.post(
            path, headers=auth_header(tokens, owner), json={"user_id": str(newcomer.id)}
        )
        assert added.status_code == 201, added.text
        again = await client.post(
            path, headers=auth_header(tokens, owner), json={"user_id": str(newcomer.id)}
        )
        assert again.status_code == 409

        # The last owner cannot step down or leave.
        demote = await client.patch(
            f"{path}/{owner.id}", headers=auth_header(tokens, owner), json={"role": "member"}
        )
        assert demote.status_code == 409
        removed = await client.delete(f"{path}/{newcomer.id}", headers=auth_header(tokens, owner))
        assert removed.status_code == 200


# ---------------------------------------------------------------------------
# Isolation in the management API
# ---------------------------------------------------------------------------
class TestIsolation:
    async def test_lists_hide_other_teams(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])

        def ids(rows: list[dict[str, Any]]) -> set[str]:
            return {r["id"] for r in rows}

        models = (await client.get("/api/v1/models?limit=200", headers=headers)).json()["items"]
        aliases = (await client.get("/api/v1/model-aliases", headers=headers)).json()
        clients = (await client.get("/api/v1/api-clients", headers=headers)).json()
        keys = (await client.get("/api/v1/api-keys", headers=headers)).json()
        agents = (await client.get("/api/v1/agents", headers=headers)).json()
        bases = (await client.get("/api/v1/knowledge-bases", headers=headers)).json()
        deployments = (await client.get("/api/v1/deployments", headers=headers)).json()

        for kind, rows, objs in (
            ("models", models, world["models"]),
            ("aliases", aliases, world["aliases"]),
            ("agents", agents, world["agents"]),
            ("bases", bases, world["bases"]),
        ):
            seen = ids(rows)
            assert str(objs["shared"].id) in seen, kind
            assert str(objs["a"].id) in seen, kind
            assert str(objs["b"].id) not in seen, kind

        client_ids = {k.client_id for k, _ in world["keys"].values()}
        assert {str(world["keys"]["a"][0].client_id)} <= ids(clients) & {str(c) for c in client_ids}
        assert str(world["keys"]["b"][0].client_id) not in ids(clients)
        assert str(world["keys"]["b"][0].id) not in ids(keys)
        assert str(world["keys"]["a"][0].id) in ids(keys)
        model_ids = {d["model_id"] for d in deployments}
        assert str(world["models"]["b"].id) not in model_ids
        assert str(world["models"]["a"].id) in model_ids

    async def test_other_teams_resources_are_not_found(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])
        b = {k: v["b"] for k, v in world.items() if isinstance(v, dict) and "b" in v}
        for path in (
            f"/api/v1/models/{b['models'].id}",
            f"/api/v1/agents/{b['agents'].id}",
            f"/api/v1/knowledge-bases/{b['bases'].id}",
        ):
            response = await client.get(path, headers=headers)
            assert response.status_code == 404, path
        assert (
            await client.delete(f"/api/v1/model-aliases/{b['aliases'].id}", headers=headers)
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/api-keys/{b['keys'][0].id}", headers=headers)
        ).status_code == 404
        minted = await client.post(
            "/api/v1/api-keys",
            headers=headers,
            json={"client_id": str(b["keys"][0].client_id), "name": "sneaky"},
        )
        assert minted.status_code == 404

    async def test_superuser_sees_every_team(
        self, client: AsyncClient, tokens, superuser: User, world: dict[str, Any]
    ) -> None:
        response = await client.get(
            f"/api/v1/models/{world['models']['b'].id}", headers=auth_header(tokens, superuser)
        )
        assert response.status_code == 200
        assert response.json()["team_id"] == str(world["b"].id)

    async def test_creating_into_a_team_requires_membership(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])
        body = {
            "name": f"new-{_suffix()}",
            "display_name": "New",
            "runtime": "mock",
            "storage_path": "/data/models/new",
        }

        refused = await client.post(
            "/api/v1/models", headers=headers, json={**body, "team_id": str(world["b"].id)}
        )
        assert refused.status_code == 403

        created = await client.post(
            "/api/v1/models", headers=headers, json={**body, "team_id": str(world["a"].id)}
        )
        assert created.status_code == 201, created.text
        assert created.json()["team_id"] == str(world["a"].id)

        client_created = await client.post(
            "/api/v1/api-clients",
            headers=headers,
            json={"name": f"app-{_suffix()}", "team_id": str(world["a"].id)},
        )
        assert client_created.status_code == 201
        assert client_created.json()["team_id"] == str(world["a"].id)

    async def test_alias_cannot_point_at_another_teams_model(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/model-aliases",
            headers=auth_header(tokens, world["alice"]),
            json={"alias": f"x-{_suffix()}", "model_id": str(world["models"]["b"].id)},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Moving resources between teams
# ---------------------------------------------------------------------------
class TestReassignment:
    async def test_member_moves_a_shared_model_into_their_team(
        self, client: AsyncClient, tokens, session: AsyncSession, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])
        model = world["models"]["shared"]
        moved = await client.put(
            "/api/v1/scope",
            headers=headers,
            json={
                "resource_type": "model",
                "resource_id": str(model.id),
                "team_id": str(world["a"].id),
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["previous_team_id"] is None
        await session.refresh(model)
        assert model.team_id == world["a"].id
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "SCOPE_ASSIGNED", AuditLog.resource_id == str(model.id)
                )
            )
        ).scalar_one()
        assert audit.meta["after"] == str(world["a"].id)

    async def test_cannot_move_into_or_out_of_a_team_you_are_not_in(
        self, client: AsyncClient, tokens, world: dict[str, Any]
    ) -> None:
        headers = auth_header(tokens, world["alice"])
        into_b = await client.put(
            "/api/v1/scope",
            headers=headers,
            json={
                "resource_type": "agent",
                "resource_id": str(world["agents"]["a"].id),
                "team_id": str(world["b"].id),
            },
        )
        assert into_b.status_code == 403
        out_of_b = await client.put(
            "/api/v1/scope",
            headers=headers,
            json={
                "resource_type": "agent",
                "resource_id": str(world["agents"]["b"].id),
                "team_id": None,
            },
        )
        assert out_of_b.status_code == 404

    async def test_requires_the_resources_manage_permission(
        self, client: AsyncClient, tokens, session: AsyncSession, settings, world: dict[str, Any]
    ) -> None:
        viewer = await _user_with(session, settings, [Perm.MODEL_VIEW], name="viewer")
        await _member(session, world["a"], viewer)
        response = await client.put(
            "/api/v1/scope",
            headers=auth_header(tokens, viewer),
            json={
                "resource_type": "model",
                "resource_id": str(world["models"]["a"].id),
                "team_id": None,
            },
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
class TestGatewayScope:
    async def test_model_list_shows_shared_and_own_team_only(
        self, gateway_app, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        _, secret = world["keys"]["a"]
        listed = await client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"})
        ids = {m["id"] for m in listed.json()["data"]}
        assert world["aliases"]["shared"].alias in ids
        assert world["aliases"]["a"].alias in ids
        assert world["aliases"]["b"].alias not in ids
        assert world["models"]["b"].name not in ids
        assert f"agent:{world['agents']['b'].slug}" not in ids

    async def test_another_teams_alias_and_model_are_not_found(
        self, gateway_app, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        _, secret = world["keys"]["a"]
        headers = {"Authorization": f"Bearer {secret}"}
        for name in (world["aliases"]["b"].alias, world["models"]["b"].name):
            response = await client.post(
                "/v1/chat/completions", headers=headers, json={"model": name, "messages": MESSAGES}
            )
            assert response.status_code == 404, name
            # The hint lists what *this* key can call, never another team's names.
            available = response.json()["error"]["details"]["available"]
            assert world["aliases"]["b"].alias not in available
        ok = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": world["aliases"]["a"].alias, "messages": MESSAGES},
        )
        assert ok.status_code == 200, ok.text

    async def test_a_shared_key_cannot_reach_team_resources(
        self, gateway_app, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        _, secret = world["keys"]["shared"]
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": world["aliases"]["a"].alias, "messages": MESSAGES},
        )
        assert response.status_code == 404

    async def test_another_teams_agent_is_not_found(
        self, gateway_app, client: AsyncClient, world: dict[str, Any]
    ) -> None:
        _, secret = world["keys"]["a"]
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": f"agent:{world['agents']['b'].slug}", "messages": MESSAGES},
        )
        assert response.status_code == 404

    async def test_team_budget_applies_to_every_key_of_the_team(
        self, gateway_app, client: AsyncClient, session: AsyncSession, world: dict[str, Any]
    ) -> None:
        team = world["a"]
        session.add(
            UsageRecord(
                endpoint="chat.completions",
                requested_model="x",
                model="x",
                prompt_tokens=500,
                team_id=team.id,
            )
        )
        session.add(GatewayQuota(scope="team", subject=str(team.id), daily_tokens=100))
        await session.flush()
        _, secret = world["keys"]["a"]
        refused = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"model": world["aliases"]["a"].alias, "messages": MESSAGES},
        )
        assert refused.status_code == 429, refused.text
        details = refused.json()["error"]["details"]
        assert details["scope"] == "team" and details["quota"] == "daily_tokens"

    async def test_team_quota_subject_must_be_a_team(
        self, client: AsyncClient, tokens, superuser: User
    ) -> None:
        response = await client.post(
            "/api/v1/quotas",
            headers=auth_header(tokens, superuser),
            json={"scope": "team", "subject": str(uuid.uuid4()), "daily_tokens": 10},
        )
        assert response.status_code == 422
