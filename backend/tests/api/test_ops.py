"""Operations and diagnostics (E7): the runner protocol, guarded requests, the support bundle."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select

from app.core.permissions import ROLE_DEFINITIONS, Role
from app.core.permissions import Permission as Perm
from app.models.audit import AuditLog
from app.models.ops import OpsJob, OpsRunner
from tests.api.conftest import _user_with
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

RUNNER_TOKEN = "test-runner-token-" + "x" * 40
BACKUP = "20261110T020000Z"
BUNDLE = "20261111T090000Z"
POINT = "20261001T120000Z"


def inventory(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "root": "/opt/ai-platform",
        "installed": {
            "created_at": "20261001T120000Z",
            "platform_version": "0.1.0",
            "format_version": 2,
        },
        "backups": [
            {
                "name": BACKUP,
                "created_at": "2026-11-10T02:00:00Z",
                "platform_version": "0.1.0",
                "schema_revision": "6a3c9e2f7b18",
                "size_bytes": 4_200_000,
                "artifacts": ["postgres.dump", "minio.tar"],
                "key_matches": True,
                "complete": True,
            },
            {"name": "20260901T020000Z", "key_matches": False, "complete": True},
        ],
        "bundles": [
            {
                "name": BUNDLE,
                "created_at": "20261111T090000Z",
                "platform_version": "0.2.0",
                "format_version": 2,
                "size_bytes": 3 * 1024**3,
                "signature": "valid",
                "signer_fingerprint": "ab:cd",
                "has_sbom": True,
            }
        ],
        "rollback_points": [{"name": POINT, "created_at": POINT, "platform_version": "0.0.9"}],
        "disk": {
            "path": "/opt/ai-platform",
            "free_bytes": 50 * 1024**3,
            "total_bytes": 200 * 1024**3,
        },
        "trust_key": {"configured": True, "fingerprint": "ab:cd"},
    }
    base.update(overrides)
    return base


@pytest.fixture
async def ops_env(session, settings, monkeypatch):
    """A configured runner token and an ops table free of rows other processes committed.

    The deletes run inside the test transaction and roll back with it, so a preview or
    another suite's committed rows are hidden for this test and untouched afterwards.
    """
    monkeypatch.setattr(settings.ops, "runner_token", SecretStr(RUNNER_TOKEN))
    await session.execute(delete(OpsJob))
    await session.execute(delete(OpsRunner))
    await session.flush()
    return settings


@pytest.fixture
async def operator(session, settings, ops_env):
    return await _user_with(
        session,
        settings,
        [Perm.BACKUP_MANAGE, Perm.BACKUP_RESTORE, Perm.SYSTEM_UPGRADE, Perm.DIAGNOSTICS_VIEW],
        name="operator",
    )


@pytest.fixture
def runner_headers() -> dict[str, str]:
    return {"X-Ops-Runner-Token": RUNNER_TOKEN}


async def beat(client, runner_headers, **inv: Any) -> Any:
    r = await client.post(
        "/api/v1/ops/runner/heartbeat",
        json={
            "name": "host-1",
            "host": "ops.local",
            "version": "1.0",
            "inventory": inventory(**inv),
        },
        headers=runner_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def claim(client, runner_headers, name: str = "host-1"):
    return await client.post(
        "/api/v1/ops/runner/claim", json={"name": name}, headers=runner_headers
    )


async def report(client, runner_headers, job_id, **body: Any):
    payload = {"name": "host-1", **body}
    return await client.post(
        f"/api/v1/ops/runner/jobs/{job_id}/report", json=payload, headers=runner_headers
    )


class TestRunnerAuthentication:
    async def test_runner_routes_are_disabled_without_a_token(self, client, settings, monkeypatch):
        monkeypatch.setattr(settings.ops, "runner_token", None)
        r = await client.post("/api/v1/ops/runner/claim", json={"name": "host-1"})
        assert r.status_code == 503

    async def test_a_placeholder_token_counts_as_unset(self, client, settings, monkeypatch):
        monkeypatch.setattr(settings.ops, "runner_token", SecretStr("change-me-ops-runner-token"))
        r = await client.post(
            "/api/v1/ops/runner/claim",
            json={"name": "h"},
            headers={"X-Ops-Runner-Token": "change-me-ops-runner-token"},
        )
        assert r.status_code == 503

    async def test_wrong_or_missing_token_is_refused(self, client, ops_env):
        for headers in ({}, {"X-Ops-Runner-Token": "nope"}):
            r = await client.post("/api/v1/ops/runner/claim", json={"name": "h"}, headers=headers)
            assert r.status_code == 401

    async def test_heartbeat_registers_the_runner_and_its_inventory(
        self, client, operator, tokens, runner_headers
    ):
        ack = await beat(client, runner_headers)
        assert ack["queued"] == 0
        r = await client.get("/api/v1/ops/runner", headers=auth_header(tokens, operator))
        body = r.json()
        assert body["online"] is True
        assert body["runner"]["name"] == "host-1"
        assert body["runner"]["inventory"]["backups"][0]["name"] == BACKUP


class TestRunnerProtocol:
    async def test_backup_lifecycle_is_audited(
        self, client, session, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        r = await client.post("/api/v1/ops/backups", json={"reason": "before change"}, headers=h)
        assert r.status_code == 202, r.text
        job = r.json()
        assert job["status"] == "QUEUED" and job["kind"] == "backup.create"

        r = await claim(client, runner_headers)
        assert r.status_code == 200
        spec = r.json()
        assert spec["id"] == job["id"] and spec["requested_by_name"] == operator.username

        r = await report(
            client, runner_headers, job["id"], status="RUNNING", log=["dumping postgres"]
        )
        assert r.json()["status"] == "RUNNING"
        r = await report(
            client,
            runner_headers,
            job["id"],
            status="SUCCEEDED",
            log=["wrote backups/20261111T100000Z", "password=hunter2"],
            result={"backup": "20261111T100000Z"},
        )
        assert r.json()["status"] == "SUCCEEDED"

        detail = (await client.get(f"/api/v1/ops/jobs/{job['id']}", headers=h)).json()
        assert detail["result"] == {"backup": "20261111T100000Z"}
        assert "dumping postgres" in detail["log"]
        assert not any("hunter2" in line for line in detail["log"])

        actions = set(
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == job["id"])
                )
            )
            .scalars()
            .all()
        )
        assert {"OPS_JOB_REQUESTED", "OPS_JOB_FINISHED"} <= actions

    async def test_only_one_job_runs_at_a_time(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        await client.post("/api/v1/ops/backups", json={}, headers=h)
        await client.post(f"/api/v1/ops/backups/{BACKUP}/verify", json={}, headers=h)
        first = await claim(client, runner_headers)
        assert first.status_code == 200
        assert (await claim(client, runner_headers)).status_code == 204
        await report(client, runner_headers, first.json()["id"], status="SUCCEEDED")
        second = await claim(client, runner_headers)
        assert second.status_code == 200 and second.json()["id"] != first.json()["id"]

    async def test_a_report_recreates_a_job_a_restore_wiped(
        self, client, session, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        job_id = str(uuid.uuid4())
        spec = {
            "id": job_id,
            "kind": "backup.restore",
            "params": {"backup": BACKUP},
            "requested_by": str(operator.id),
            "requested_by_name": operator.username,
            "reason": "disaster drill",
            "created_at": "2026-11-11T08:00:00Z",
        }
        r = await report(
            client, runner_headers, job_id, status="SUCCEEDED", log=["restored"], job=spec
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "SUCCEEDED"
        assert r.json()["requested_by_name"] == operator.username
        # Replayed buffered reports after the restore change nothing.
        again = await report(
            client, runner_headers, job_id, status="FAILED", error="late", job=spec
        )
        assert again.json()["status"] == "SUCCEEDED"

    async def test_a_report_for_an_unknown_job_without_spec_is_404(
        self, client, ops_env, runner_headers
    ):
        r = await report(client, runner_headers, uuid.uuid4(), status="RUNNING")
        assert r.status_code == 404


class TestGuardedRequests:
    async def test_names_must_come_from_the_runner_inventory(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        r = await client.post("/api/v1/ops/backups/20200101T000000Z/verify", json={}, headers=h)
        assert r.status_code == 404
        r = await client.post("/api/v1/ops/backups/..%2Fetc/verify", json={}, headers=h)
        assert r.status_code in (404, 422)

    async def test_restore_needs_the_name_typed_back_and_a_matching_key(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        url = f"/api/v1/ops/backups/{BACKUP}/restore"
        r = await client.post(url, json={"confirm": "yes", "reason": "drill"}, headers=h)
        assert r.status_code == 422
        r = await client.post(
            "/api/v1/ops/backups/20260901T020000Z/restore",
            json={"confirm": "20260901T020000Z", "reason": "drill"},
            headers=h,
        )
        assert r.status_code == 422 and "encryption key" in r.text
        r = await client.post(url, json={"confirm": BACKUP, "reason": "drill"}, headers=h)
        assert r.status_code == 202, r.text
        # Nothing else may be queued beside a restore.
        r = await client.post("/api/v1/ops/backups", json={}, headers=h)
        assert r.status_code == 409

    async def test_queued_jobs_can_be_cancelled_running_ones_cannot(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        a = (await client.post("/api/v1/ops/backups", json={}, headers=h)).json()
        b = (await client.post(f"/api/v1/ops/backups/{BACKUP}/verify", json={}, headers=h)).json()
        # Both rows share the test transaction's now(), so either may be claimed first.
        running = (await claim(client, runner_headers)).json()["id"]
        queued = b["id"] if running == a["id"] else a["id"]
        r = await client.post(f"/api/v1/ops/jobs/{running}/cancel", headers=h)
        assert r.status_code == 409
        r = await client.post(f"/api/v1/ops/jobs/{queued}/cancel", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"


class TestUpgrades:
    async def test_preflight_requires_a_verified_bundle(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        pf = (await client.get(f"/api/v1/ops/bundles/{BUNDLE}/preflight", headers=h)).json()
        checks = {c["id"]: c["status"] for c in pf["checks"]}
        assert pf["ok"] is False and checks["verified"] == "fail"
        assert (
            checks["signature"] == "pass"
            and checks["version"] == "pass"
            and checks["disk"] == "pass"
        )

        job = (await client.post(f"/api/v1/ops/bundles/{BUNDLE}/verify", json={}, headers=h)).json()
        await claim(client, runner_headers)
        await report(client, runner_headers, job["id"], status="SUCCEEDED")
        pf = (await client.get(f"/api/v1/ops/bundles/{BUNDLE}/preflight", headers=h)).json()
        assert pf["ok"] is True, pf

        body = {"bundle": BUNDLE, "confirm": "wrong", "reason": "monthly update"}
        assert (await client.post("/api/v1/ops/upgrades", json=body, headers=h)).status_code == 422
        body["confirm"] = BUNDLE
        r = await client.post("/api/v1/ops/upgrades", json=body, headers=h)
        assert r.status_code == 202, r.text
        assert r.json()["params"] == {"bundle": BUNDLE, "migrate": True}

    async def test_preflight_blocks_an_invalid_or_unsigned_bundle(
        self, client, operator, tokens, runner_headers
    ):
        h = auth_header(tokens, operator)
        for signature in ("invalid", "unsigned"):
            bundles = [dict(inventory()["bundles"][0], signature=signature)]
            await beat(client, runner_headers, bundles=bundles)
            pf = (await client.get(f"/api/v1/ops/bundles/{BUNDLE}/preflight", headers=h)).json()
            sig = next(c for c in pf["checks"] if c["id"] == "signature")
            # Unsigned fails because this host has a trust key installed.
            assert sig["status"] == "fail", (signature, sig)
            body = {"bundle": BUNDLE, "confirm": BUNDLE, "reason": "update"}
            r = await client.post("/api/v1/ops/upgrades", json=body, headers=h)
            assert r.status_code == 422

    async def test_an_older_bundle_is_refused(self, client, operator, tokens, runner_headers):
        bundles = [dict(inventory()["bundles"][0], platform_version="0.0.1")]
        await beat(client, runner_headers, bundles=bundles)
        h = auth_header(tokens, operator)
        pf = (await client.get(f"/api/v1/ops/bundles/{BUNDLE}/preflight", headers=h)).json()
        assert next(c for c in pf["checks"] if c["id"] == "version")["status"] == "fail"

    async def test_rollback_needs_a_reported_point(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers)
        h = auth_header(tokens, operator)
        r = await client.post(
            "/api/v1/ops/rollbacks",
            json={"point": "20200101T000000Z", "confirm": "20200101T000000Z", "reason": "bad"},
            headers=h,
        )
        assert r.status_code == 404
        r = await client.post(
            "/api/v1/ops/rollbacks",
            json={"point": POINT, "confirm": POINT, "reason": "bad"},
            headers=h,
        )
        assert r.status_code == 202


class TestPermissions:
    async def test_permissions_are_separated(self, client, session, settings, tokens, ops_env):
        backup_only = await _user_with(session, settings, [Perm.BACKUP_MANAGE], name="bk")
        h = auth_header(tokens, backup_only)
        assert (await client.get("/api/v1/ops/backups", headers=h)).status_code == 200
        r = await client.post(
            f"/api/v1/ops/backups/{BACKUP}/restore",
            json={"confirm": BACKUP, "reason": "x" * 5},
            headers=h,
        )
        assert r.status_code == 403
        assert (await client.get("/api/v1/ops/bundles", headers=h)).status_code == 403

    def test_admin_cannot_restore_or_upgrade_by_default(self):
        admin = ROLE_DEFINITIONS[Role.ADMIN][1]
        assert Perm.BACKUP_RESTORE not in admin and Perm.SYSTEM_UPGRADE not in admin
        assert Perm.DIAGNOSTICS_VIEW in ROLE_DEFINITIONS[Role.AUDITOR][1]
        assert Perm.BACKUP_RESTORE not in ROLE_DEFINITIONS[Role.INFRA_ADMIN][1]


class TestDiagnostics:
    async def test_inventory_reports_schema_and_components(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers)
        r = await client.get("/api/v1/diagnostics/inventory", headers=auth_header(tokens, operator))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_revision"] and body["schema_head"]
        names = {c["component"] for c in body["components"]}
        assert {"control-plane", "postgres", "valkey", "qdrant", "ops-runner"} <= names
        assert body["installed_bundle"]["created_at"] == "20261001T120000Z"

    async def test_config_is_redacted(self, client, operator, tokens, settings):
        r = await client.get("/api/v1/diagnostics/config", headers=auth_header(tokens, operator))
        body = r.json()
        assert body["redacted_fields"] > 0
        assert body["config"]["database"]["password"] == "[REDACTED]"
        assert body["config"]["ops"]["runner_token"] == "[REDACTED]"
        assert settings.database.password.get_secret_value() not in r.text

    async def test_support_bundle_contains_no_secrets(
        self, client, session, operator, tokens, settings, runner_headers
    ):
        await beat(client, runner_headers)
        r = await client.post(
            "/api/v1/diagnostics/bundle?logs=false", headers=auth_header(tokens, operator)
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/zip"
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        names = archive.namelist()
        assert any(n.endswith("/manifest.json") for n in names)
        assert any(n.endswith("/config.json") for n in names)
        blob = "\n".join(archive.read(n).decode() for n in names)
        secrets = [
            settings.database.password.get_secret_value(),
            settings.auth.jwt_secret_key.get_secret_value(),
            settings.security.encryption_key.get_secret_value(),
            RUNNER_TOKEN,
        ]
        for secret in secrets:
            assert secret not in blob
        manifest = json.loads(archive.read(next(n for n in names if n.endswith("manifest.json"))))
        assert manifest["generated_by"] == operator.username
        exported = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "DIAGNOSTICS_EXPORTED", AuditLog.user_id == operator.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(exported) == 1


class TestPartialRestore:
    """E60: restoring only some parts of a backup."""

    OTHER_KEY = "20260901T020000Z"

    def _backups(self) -> list[dict[str, Any]]:
        return [
            {
                "name": BACKUP,
                "artifacts": ["minio.tar", "postgres.dump", "qdrant"],
                "parts": ["database", "files", "vectors"],
                "key_matches": True,
                "complete": True,
            },
            {
                "name": self.OTHER_KEY,
                "artifacts": ["minio.tar", "postgres.dump", "qdrant"],
                "parts": ["database", "files"],
                "key_matches": False,
                "complete": True,
            },
        ]

    async def test_parts_reach_the_runner(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        h = auth_header(tokens, operator)
        r = await client.post(
            f"/api/v1/ops/backups/{BACKUP}/restore",
            json={"confirm": BACKUP, "reason": "vectors only", "parts": ["vectors", "files"]},
            headers=h,
        )
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        # Ordered as backup.py restores them.
        assert spec["params"] == {"backup": BACKUP, "parts": ["files", "vectors"]}

    async def test_every_part_is_a_whole_restore(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        h = auth_header(tokens, operator)
        r = await client.post(
            f"/api/v1/ops/backups/{BACKUP}/restore",
            json={"confirm": BACKUP, "reason": "drill", "parts": ["database", "files", "vectors"]},
            headers=h,
        )
        assert r.status_code == 202, r.text
        assert (await claim(client, runner_headers)).json()["params"] == {"backup": BACKUP}

    async def test_a_part_the_backup_lacks_is_refused(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers, backups=self._backups())
        r = await client.post(
            f"/api/v1/ops/backups/{self.OTHER_KEY}/restore",
            json={"confirm": self.OTHER_KEY, "reason": "drill", "parts": ["vectors"]},
            headers=auth_header(tokens, operator),
        )
        assert r.status_code == 422 and "holds no vectors" in r.text

    async def test_another_key_still_restores_files(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        h = auth_header(tokens, operator)
        url = f"/api/v1/ops/backups/{self.OTHER_KEY}/restore"
        r = await client.post(
            url,
            json={"confirm": self.OTHER_KEY, "reason": "drill", "parts": ["database"]},
            headers=h,
        )
        assert r.status_code == 422 and "encryption key" in r.text
        r = await client.post(
            url, json={"confirm": self.OTHER_KEY, "reason": "drill", "parts": ["files"]}, headers=h
        )
        assert r.status_code == 202, r.text

    async def test_unknown_parts_are_refused(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        r = await client.post(
            f"/api/v1/ops/backups/{BACKUP}/restore",
            json={"confirm": BACKUP, "reason": "drill", "parts": ["everything"]},
            headers=auth_header(tokens, operator),
        )
        assert r.status_code == 422


class TestScopedRestore:
    """E64: restoring only some knowledge bases, or one team's."""

    KB_A = "11111111-1111-4111-8111-111111111111"
    KB_B = "22222222-2222-4222-8222-222222222222"
    TEAM = "33333333-3333-4333-8333-333333333333"
    LONE_TEAM = "55555555-5555-4555-8555-555555555555"
    OTHER_KEY = "20260901T020000Z"
    OLD = "20260801T020000Z"

    def _backups(self) -> list[dict[str, Any]]:
        catalogue = {
            "knowledge_bases": [
                {
                    "id": self.KB_A,
                    "name": "policies",
                    "display_name": "Policies",
                    "team_id": self.TEAM,
                },
                {"id": self.KB_B, "name": "faq", "display_name": "FAQ", "team_id": None},
            ],
            "teams": [{"id": self.TEAM, "name": "legal"}],
            "catalogued": True,
        }
        whole = {"artifacts": ["minio.tar", "postgres.dump", "qdrant"], "complete": True}
        return [
            {
                **whole,
                "name": BACKUP,
                "parts": ["database", "files", "vectors"],
                "key_matches": True,
                **catalogue,
            },
            {
                **whole,
                "name": self.OTHER_KEY,
                "parts": ["database", "files", "vectors"],
                "key_matches": False,
                **catalogue,
            },
            # Taken before E64: no catalogue, so the ids can't be checked here.
            {**whole, "name": self.OLD, "parts": ["database", "files"], "key_matches": True},
        ]

    async def _restore(self, client, tokens, operator, name: str, **body: Any):
        return await client.post(
            f"/api/v1/ops/backups/{name}/restore",
            json={"confirm": name, "reason": "deleted by mistake", **body},
            headers=auth_header(tokens, operator),
        )

    async def test_the_catalogue_is_listed(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        r = await client.get("/api/v1/ops/backups", headers=auth_header(tokens, operator))
        assert r.status_code == 200, r.text
        item = next(b for b in r.json()["items"] if b["name"] == BACKUP)
        assert item["catalogued"] is True
        assert [b["name"] for b in item["knowledge_bases"]] == ["policies", "faq"]
        assert item["teams"][0]["name"] == "legal"
        old = next(b for b in r.json()["items"] if b["name"] == self.OLD)
        assert old["catalogued"] is False and old["knowledge_bases"] == []

    async def test_knowledge_bases_reach_the_runner(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client,
            tokens,
            operator,
            BACKUP,
            knowledge_base_ids=[self.KB_B, self.KB_A],
            parts=["database", "vectors"],
        )
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        assert spec["params"] == {
            "backup": BACKUP,
            "parts": ["database", "vectors"],
            "knowledge_bases": sorted([self.KB_A, self.KB_B]),
        }

    async def test_a_team_reaches_the_runner(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(client, tokens, operator, BACKUP, team_id=self.TEAM)
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        assert spec["params"] == {"backup": BACKUP, "team": self.TEAM}

    async def test_exact_members_reach_the_runner(self, client, operator, tokens, runner_headers):
        """E91: the team's members exactly as in the backup; only with its setup."""
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client, tokens, operator, BACKUP, team_id=self.TEAM, exact_members=True
        )
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        assert spec["params"] == {"backup": BACKUP, "team": self.TEAM, "exact_members": True}
        r = await self._restore(
            client,
            tokens,
            operator,
            BACKUP,
            team_id=self.TEAM,
            exact_members=True,
            knowledge_only=True,
        )
        assert r.status_code == 422
        r = await self._restore(
            client, tokens, operator, BACKUP, knowledge_base_ids=[self.KB_A], exact_members=True
        )
        assert r.status_code == 422

    async def test_a_team_setup_needs_the_backups_key(
        self, client, operator, tokens, runner_headers
    ):
        """E68: a team's setup holds encrypted credentials; its knowledge bases don't."""
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(client, tokens, operator, self.OTHER_KEY, team_id=self.TEAM)
        assert r.status_code == 422 and "knowledge_only" in r.text
        r = await self._restore(
            client, tokens, operator, self.OTHER_KEY, team_id=self.TEAM, knowledge_only=True
        )
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        assert spec["params"] == {
            "backup": self.OTHER_KEY,
            "team": self.TEAM,
            "knowledge_only": True,
        }

    async def test_a_team_without_knowledge_bases(self, client, operator, tokens, runner_headers):
        """E68: its setup can be restored; knowledge_only would have nothing to do."""
        backups = self._backups()
        backups[0]["teams"].append({"id": self.LONE_TEAM, "name": "ops"})
        await beat(client, runner_headers, backups=backups)
        r = await self._restore(
            client, tokens, operator, BACKUP, team_id=self.LONE_TEAM, knowledge_only=True
        )
        assert r.status_code == 422 and "no knowledge bases" in r.text
        r = await self._restore(client, tokens, operator, BACKUP, team_id=self.LONE_TEAM)
        assert r.status_code == 202, r.text

    async def test_files_alone_need_no_key(self, client, operator, tokens, runner_headers):
        """Restoring only a team's files touches no setup, so any key will do."""
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client, tokens, operator, self.OTHER_KEY, team_id=self.TEAM, parts=["files"]
        )
        assert r.status_code == 202, r.text

    async def test_knowledge_only_needs_a_team(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client, tokens, operator, BACKUP, knowledge_base_ids=[self.KB_A], knowledge_only=True
        )
        assert r.status_code == 422

    async def test_unknown_ids_are_refused(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, backups=self._backups())
        stranger = "44444444-4444-4444-8444-444444444444"
        r = await self._restore(client, tokens, operator, BACKUP, knowledge_base_ids=[stranger])
        assert r.status_code == 422 and "no knowledge base" in r.text
        r = await self._restore(client, tokens, operator, BACKUP, team_id=stranger)
        assert r.status_code == 422 and "team is not in this backup" in r.text

    async def test_bases_and_a_team_together_are_refused(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client, tokens, operator, BACKUP, knowledge_base_ids=[self.KB_A], team_id=self.TEAM
        )
        assert r.status_code == 422

    async def test_another_key_is_fine_for_a_scoped_restore(
        self, client, operator, tokens, runner_headers
    ):
        """The knowledge base tables hold nothing encrypted with the key."""
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(
            client, tokens, operator, self.OTHER_KEY, knowledge_base_ids=[self.KB_A]
        )
        assert r.status_code == 202, r.text

    async def test_an_uncatalogued_backup_is_left_to_the_runner(
        self, client, operator, tokens, runner_headers
    ):
        await beat(client, runner_headers, backups=self._backups())
        r = await self._restore(client, tokens, operator, self.OLD, knowledge_base_ids=[self.KB_A])
        assert r.status_code == 202, r.text

    async def test_needs_the_dump(self, client, operator, tokens, runner_headers):
        backups = self._backups()
        backups[0]["parts"] = ["files", "vectors"]
        await beat(client, runner_headers, backups=backups)
        r = await self._restore(
            client, tokens, operator, BACKUP, knowledge_base_ids=[self.KB_A], parts=["files"]
        )
        assert r.status_code == 422 and "database dump" in r.text

    async def test_needs_backup_restore(
        self, client, tokens, runner_headers, session, settings, ops_env
    ):
        await beat(client, runner_headers, backups=self._backups())
        viewer = await _user_with(session, settings, [Perm.BACKUP_MANAGE], name="viewer")
        r = await self._restore(client, tokens, viewer, BACKUP, knowledge_base_ids=[self.KB_A])
        assert r.status_code == 403


class TestTrustChange:
    """E63: retiring or revoking a bundle signing key from the console."""

    OLD = "sha256:" + "a" * 64
    NEW = "sha256:" + "b" * 64
    CHILD = "sha256:" + "c" * 64

    def _trust(self, *, accepted: list[str], keys: list[dict[str, Any]]) -> dict[str, Any]:
        return {"configured": True, "fingerprint": accepted[0], "accepted": accepted, "keys": keys}

    def _two_keys(self) -> dict[str, Any]:
        return self._trust(
            accepted=[self.OLD, self.NEW],
            keys=[
                {"fingerprint": self.OLD, "status": "trusted", "source": "installed"},
                {"fingerprint": self.NEW, "status": "trusted", "source": "installed"},
            ],
        )

    async def test_a_revoke_reaches_the_runner(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, trust_key=self._two_keys())
        r = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.OLD,
                "action": "revoke",
                "confirm": "revoke",
                "reason": "leaked",
            },
            headers=auth_header(tokens, operator),
        )
        assert r.status_code == 202, r.text
        spec = (await claim(client, runner_headers)).json()
        assert spec["kind"] == "trust.change"
        assert spec["params"] == {"fingerprint": self.OLD, "action": "revoke", "reason": "leaked"}

    async def test_the_action_must_be_typed_back(self, client, operator, tokens, runner_headers):
        await beat(client, runner_headers, trust_key=self._two_keys())
        r = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.OLD,
                "action": "retire",
                "confirm": "yes",
                "reason": "rotated",
            },
            headers=auth_header(tokens, operator),
        )
        assert r.status_code == 422

    async def test_the_last_key_is_refused(self, client, operator, tokens, runner_headers):
        trust = self._trust(
            accepted=[self.OLD, self.CHILD],
            keys=[
                {"fingerprint": self.OLD, "status": "trusted", "source": "installed"},
                # Added on OLD's authority, so revoking OLD revokes it too.
                {
                    "fingerprint": self.CHILD,
                    "status": "trusted",
                    "source": "update",
                    "added_by": self.OLD,
                },
            ],
        )
        await beat(client, runner_headers, trust_key=trust)
        h = auth_header(tokens, operator)
        revoke = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.OLD,
                "action": "revoke",
                "confirm": "revoke",
                "reason": "leaked",
            },
            headers=h,
        )
        assert revoke.status_code == 422 and "no trusted key" in revoke.text
        # Retiring doesn't cascade, so the child remains and it is allowed.
        retire = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.OLD,
                "action": "retire",
                "confirm": "retire",
                "reason": "rotated",
            },
            headers=h,
        )
        assert retire.status_code == 202, retire.text

    async def test_unknown_or_already_ended_keys(self, client, operator, tokens, runner_headers):
        trust = self._two_keys()
        trust["keys"][1]["status"] = "retired"
        trust["accepted"] = [self.OLD]
        await beat(client, runner_headers, trust_key=trust)
        h = auth_header(tokens, operator)
        unknown = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": "sha256:" + "d" * 64,
                "action": "retire",
                "confirm": "retire",
                "reason": "x y z",
            },
            headers=h,
        )
        assert unknown.status_code == 404
        again = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.NEW,
                "action": "retire",
                "confirm": "retire",
                "reason": "again",
            },
            headers=h,
        )
        assert again.status_code == 409

    async def test_needs_system_upgrade(
        self, client, tokens, session, settings, runner_headers, ops_env
    ):
        await beat(client, runner_headers, trust_key=self._two_keys())
        viewer = await _user_with(session, settings, [Perm.DIAGNOSTICS_VIEW], name="e63view")
        r = await client.post(
            "/api/v1/ops/trust/keys",
            json={
                "fingerprint": self.OLD,
                "action": "revoke",
                "confirm": "revoke",
                "reason": "leaked",
            },
            headers=auth_header(tokens, viewer),
        )
        assert r.status_code == 403
