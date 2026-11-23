"""E64: restoring single knowledge bases (or one team's) from a backup.

``backup.py`` is driven with docker, psql and Qdrant replaced by fakes; what the SQL does
to a real database is in ``tests/api/test_scoped_restore_sql.py``.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_release_tooling import load

KB_A = "11111111-1111-4111-8111-111111111111"
KB_B = "22222222-2222-4222-8222-222222222222"
TEAM = "33333333-3333-4333-8333-333333333333"


class Done:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def backup(monkeypatch, tmp_path):
    module = load("backup")
    state = SimpleNamespace(
        calls=[], requests=[], scripts=[], queries=[], live={}, apply=Done(), module=module
    )
    columns = (
        "knowledge_bases|id,name,collection,team_id\n"
        "documents|id,knowledge_base_id,storage_key,uploaded_by\n"
        "document_chunks|id,document_id,ordinal,text\n"
    )

    def psql(user, database, sql="", *, script=None):
        if script is not None:
            state.scripts.append((database, script))
            return state.apply if database == "ai_platform" else Done()
        state.queries.append((database, sql))
        if "information_schema.columns" in sql:
            return Done(columns)
        if "pg_constraint" in sql:
            return Done("documents|uploaded_by|users|id|true\n")
        if "FROM public.knowledge_bases k" in sql and database != "ai_platform":
            rows = {
                KB_A: f"{KB_A}|policies|kb_{KB_A[:8]}|{TEAM}",
                KB_B: f"{KB_B}|faq|kb_{KB_B[:8]}|",
            }
            wanted = [k for k, row in rows.items() if k in sql or row.split("|")[1] in sql]
            if "JOIN public.teams" in sql:
                wanted = [KB_A] if TEAM in sql or "legal" in sql else []
            return Done("".join(rows[k] + "\n" for k in wanted))
        if sql.startswith("SELECT id, collection"):
            return Done("".join(f"{k}|{c}\n" for k, c in state.live.items()))
        return Done()

    def compose(*args, **_):
        state.calls.append(args)
        return Done("ok")

    def qdrant(method, suffix, body=None):
        state.requests.append((method, suffix, body))
        return True

    data_root = tmp_path / "data"
    monkeypatch.setattr(module, "verify", lambda path: 0)
    monkeypatch.setattr(module, "_psql", psql)
    monkeypatch.setattr(module, "_compose", compose)
    monkeypatch.setattr(module, "_qdrant_request", qdrant)
    monkeypatch.setattr(module, "_wait_for_backend", lambda seconds=120: True)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(
        module,
        "_env",
        lambda: {"PLATFORM_DATA_ROOT": str(data_root), "SECURITY__ENCRYPTION_KEY": "k1"},
    )
    state.data_root = data_root
    return state


def make_backup(module, tmp_path: Path, *, key: str = "k1") -> Path:
    path = tmp_path / "backups" / "20261110T020000Z"
    (path / "qdrant").mkdir(parents=True)
    (path / "postgres.dump").write_bytes(b"dump")
    (path / "qdrant" / f"kb_{KB_A[:8]}.snapshot").write_bytes(b"snap")
    source = tmp_path / "src" / "minio" / "bucket" / "documents"
    for kb in (KB_A, KB_B):
        obj = source / kb / "doc-1-abc" / "xl.meta"
        obj.parent.mkdir(parents=True)
        obj.write_text(f"backup copy of {kb}")
    (tmp_path / "src" / "minio" / "bucket" / "other.txt").write_text("not a document")
    with tarfile.open(path / "minio.tar", "w") as archive:
        archive.add(tmp_path / "src" / "minio", arcname="minio")
    manifest = {
        "created_at": "20261110T020000Z",
        "encryption_key_fingerprint": module._key_fingerprint({"SECURITY__ENCRYPTION_KEY": key}),
        "database": {"user": "ai_platform", "name": "ai_platform"},
        "artifacts": {
            "postgres.dump": {},
            "minio.tar": {},
            "qdrant": {"collections": [f"kb_{KB_A[:8]}"], "snapshots": 1},
        },
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    return path


def live_files(data_root: Path, *, kb: str = KB_A, text: str = "live copy") -> Path:
    obj = data_root / "minio" / "bucket" / "documents" / kb / "doc-2-new" / "xl.meta"
    obj.parent.mkdir(parents=True)
    obj.write_text(text)
    (data_root / "minio" / "bucket" / "unrelated.txt").write_text("keep me")
    return obj


class TestSql:
    def test_in_scope(self):
        in_scope = load("backup").in_scope
        assert in_scope(f"minio/bucket/documents/{KB_A}/doc/xl.meta", {KB_A})
        assert not in_scope(f"minio/bucket/documents/{KB_B}/doc/xl.meta", {KB_A})
        assert not in_scope(f"minio/bucket/documents/{KB_A}", {KB_A})  # the directory itself
        assert not in_scope("minio/.minio.sys/buckets/bucket/x", {KB_A})

    def test_values_are_quoted(self):
        sql = load("backup").resolve_scope_sql(["it's"], None)
        assert "'it''s'" in sql and "it's'" not in sql.replace("'it''s'", "")

    def test_common_columns_keep_the_live_order_and_drop_fresh_ones(self):
        common = load("backup").common_columns(
            {"knowledge_bases": ["id", "name", "added_later"], "document_chunks": ["id", "text"]},
            {
                "knowledge_bases": ["name", "id", "dropped_since"],
                "document_chunks": ["text", "id"],
                "documents": ["id"],
            },
        )
        assert common == {"knowledge_bases": ["id", "name"], "document_chunks": ["text"]}

    def test_the_script_stages_loads_then_applies(self):
        module = load("backup")
        columns = {"knowledge_bases": ["id", "name"], "documents": ["id", "knowledge_base_id"]}
        script = module.apply_script([KB_A], columns, [], {"knowledge_bases", "documents"})
        lines = script.splitlines()
        copies = [ln for ln in lines if ln.startswith("\\copy")]
        assert len(copies) == 2 and not any(ln.endswith(";") for ln in copies)
        assert lines.index(copies[0]) > max(
            i for i, ln in enumerate(lines) if ln.startswith("CREATE TEMP TABLE")
        )
        delete = next(
            i for i, ln in enumerate(lines) if ln.startswith("DELETE FROM public.knowledge_bases")
        )
        assert delete > lines.index(copies[-1])
        assert lines[-1].startswith("INSERT INTO public.documents")


class TestRestoreScoped:
    def test_one_base_rows_files_and_vectors(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        live = live_files(backup.data_root)
        backup.live = {KB_A: "kb_reindexed"}
        assert backup.module.restore(path, True, knowledge_bases=["policies"]) == 0

        # Rows: exported from the scratch database, applied to the live one.
        (scratch, export), (target, apply) = backup.scripts
        assert scratch.startswith("ai_platform_scoped_") and target == "ai_platform"
        assert f"'{KB_A}'::uuid" in export and KB_B not in export
        assert 'UPDATE stage_documents SET "uploaded_by" = NULL' in apply
        # Files: only this base's, merged; its current objects moved aside, the rest kept.
        restored = (
            backup.data_root / "minio" / "bucket" / "documents" / KB_A / "doc-1-abc" / "xl.meta"
        )
        assert restored.read_text() == f"backup copy of {KB_A}"
        assert not live.exists()
        assert not (backup.data_root / "minio" / "bucket" / "documents" / KB_B).exists()
        assert (backup.data_root / "minio" / "bucket" / "unrelated.txt").exists()
        assert not (backup.data_root / "minio" / "bucket" / "other.txt").exists()
        aside = next(p for p in backup.data_root.iterdir() if p.name.startswith("minio.before-"))
        assert (aside / "bucket" / "documents" / KB_A / "doc-2-new" / "xl.meta").exists()
        # Vectors: the backup's collection recovered; the one the base used since dropped.
        recover = [r for r in backup.requests if r[0] == "PUT"]
        assert recover == [("PUT", f"/collections/kb_{KB_A[:8]}/snapshots/recover", recover[0][2])]
        assert ("DELETE", "/collections/kb_reindexed", None) in backup.requests
        # The API kept running; the scratch database was dropped.
        assert ("stop", "backend") not in backup.calls
        assert any(c[:4] == ("exec", "-T", "postgres", "dropdb") for c in backup.calls)

    def test_a_team_means_its_bases(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        assert (
            backup.module.restore(path, True, parts=["database"], team="legal", knowledge_only=True)
            == 0
        )
        export = backup.scripts[0][1]
        assert KB_A in export and KB_B not in export
        assert not [r for r in backup.requests if r[0] == "PUT"]

    def test_unknown_names_are_refused_before_anything_changes(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        assert backup.module.restore(path, True, knowledge_bases=["nope"]) == 1
        assert backup.module.restore(path, True, team="nobody") == 1
        assert not backup.scripts
        assert any(c[:4] == ("exec", "-T", "postgres", "dropdb") for c in backup.calls)

    def test_a_refusal_in_the_transaction_stops_the_rest(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        live = live_files(backup.data_root)
        backup.apply = Done(
            returncode=3,
            stderr=(
                "ERROR:  SCOPED-REFUSED: the team that owned a knowledge base being restored no "
                "longer exists.\nCONTEXT: PL/pgSQL"
            ),
        )
        assert backup.module.restore(path, True, knowledge_bases=[KB_A]) == 1
        assert live.read_text() == "live copy"
        assert not [r for r in backup.requests if r[0] == "PUT"]

    def test_files_alone_skip_a_base_that_is_not_here(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        backup.live = {}
        assert backup.module.restore(path, True, parts=["files"], knowledge_bases=[KB_A]) == 0
        assert not (backup.data_root / "minio" / "bucket" / "documents" / KB_A).exists()
        assert not backup.scripts

    def test_vectors_alone_go_into_the_live_collection(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path)
        backup.live = {KB_A: "kb_reindexed"}
        assert backup.module.restore(path, True, parts=["vectors"], knowledge_bases=[KB_A]) == 0
        assert [r[1] for r in backup.requests] == ["/collections/kb_reindexed/snapshots/recover"]

    def test_another_key_does_not_matter(self, backup, tmp_path):
        path = make_backup(backup.module, tmp_path, key="k-old")
        assert backup.module.restore(path, True, parts=["database"], knowledge_bases=[KB_A]) == 0

    def test_the_catalogue_is_recorded(self, backup):
        backup.module._psql = lambda *a, **k: Done('notice\n{"knowledge_bases": [], "teams": []}\n')
        assert backup.module._catalogue("u", "d") == {"knowledge_bases": [], "teams": []}
        backup.module._psql = lambda *a, **k: Done("", returncode=2)
        assert backup.module._catalogue("u", "d") is None


class TestRunner:
    def test_scope_arguments_are_ids_only(self):
        runner = load("ops_runner")
        assert runner.restore_scope({"team": TEAM}) == ["--team", TEAM]
        assert runner.restore_scope({"team": "legal; rm -rf /"}) == []
        assert runner.restore_scope({"knowledge_bases": [KB_A, "--yes", KB_B]}) == [
            "--knowledge-base",
            KB_A,
            "--knowledge-base",
            KB_B,
        ]

    def test_catalogue_from_the_manifest(self):
        runner = load("ops_runner")
        manifest = {
            "catalogue": {
                "knowledge_bases": [
                    {"id": KB_A, "name": "policies", "team_id": TEAM},
                    {"id": "not-a-uuid", "name": "x"},
                ],
                "teams": [{"id": TEAM, "name": "legal"}],
            }
        }
        out = runner.restore_catalogue(manifest)
        assert out["catalogued"] is True
        assert out["knowledge_bases"] == [
            {"id": KB_A, "name": "policies", "display_name": "policies", "team_id": TEAM}
        ]
        assert runner.restore_catalogue({}) == {
            "knowledge_bases": [],
            "teams": [],
            "catalogued": False,
        }


class TestTeamSetup:
    """E68: ``--team`` brings back the team's setup with its knowledge bases."""

    TEAM_COLUMNS = (
        "teams|id,name,slug\n"
        "team_memberships|id,team_id,user_id\n"
        "agents|id,slug,team_id,owner_id\n"
        "api_keys|id,client_id,key_hash,revoked_at\n"
    )

    def _fake(self, backup, *, team_row: bool = True):
        plain = backup.module._psql

        def psql(user, database, sql="", *, script=None):
            if script is None and "information_schema.columns" in sql and "'teams'" in sql:
                backup.queries.append((database, sql))
                return Done(self.TEAM_COLUMNS)
            if script is None and "UNION ALL" in sql:
                return Done("teams|1\nagents|2\napi_keys|1\n")
            if script is None and "FROM public.teams WHERE id::text" in sql:
                backup.queries.append((database, sql))
                return Done(f"{TEAM}|legal\n" if team_row else "")
            if script is None and "indisunique" in sql:
                return Done("agents|slug\nteams|name\n")
            if script is None and "pg_constraint" in sql and "'teams'" in sql:
                return Done(
                    "agents|owner_id|users|id|true\nteam_memberships|user_id|users|id|false\n"
                )
            return plain(user, database, sql, script=script)

        backup.module._psql = psql

    def test_setup_and_bases_in_one_transaction(self, backup, tmp_path):
        self._fake(backup)
        path = make_backup(backup.module, tmp_path)
        assert backup.module.restore(path, True, parts=["database"], team="legal") == 0
        (scratch, export), (target, apply) = backup.scripts
        assert scratch.startswith("ai_platform_scoped_") and target == "ai_platform"
        assert "aip-team/agents.csv" in export and KB_A in export
        # One script: the team's rows first, so a base's team exists when it is checked.
        assert apply.index("INSERT INTO public.teams") < apply.index(
            "DELETE FROM public.knowledge_bases"
        )
        assert "ON CONFLICT (id) DO UPDATE" in apply
        assert "INSERT INTO public.team_memberships" in apply and "ON CONFLICT DO NOTHING" in apply
        assert "DELETE FROM stage_team_memberships" in apply  # a member deleted since
        assert 'UPDATE stage_agents SET "owner_id" = NULL' in apply
        assert "UPDATE stage_api_keys s SET revoked_at = now()" in apply
        assert 'public.agents l ON l.id <> s.id AND l."slug" = s."slug"' in apply

    def test_a_team_with_no_bases_still_has_its_setup(self, backup, tmp_path, monkeypatch):
        self._fake(backup)
        path = make_backup(backup.module, tmp_path)
        monkeypatch.setattr(
            backup.module,
            "_resolve_scope",
            lambda *a, **k: (_ for _ in ()).throw(
                ValueError("the backup has no knowledge bases owned by team 'legal'")
            ),
        )
        assert backup.module.restore(path, True, parts=["database"], team="legal") == 0
        export = backup.scripts[0][1]
        assert "aip-team/teams.csv" in export and "knowledge_bases" not in export

    def test_an_unknown_team_is_refused(self, backup, tmp_path):
        self._fake(backup, team_row=False)
        path = make_backup(backup.module, tmp_path)
        assert backup.module.restore(path, True, team="nobody") == 1
        assert not backup.scripts

    def test_another_key_refuses_the_setup_but_not_the_bases(self, backup, tmp_path):
        self._fake(backup)
        path = make_backup(backup.module, tmp_path, key="k-old")
        assert backup.module.restore(path, True, parts=["database"], team="legal") == 1
        assert not backup.scripts
        assert (
            backup.module.restore(path, True, parts=["database"], team="legal", knowledge_only=True)
            == 0
        )
        assert "aip-team" not in backup.scripts[0][1]

    def test_files_alone_restore_no_setup(self, backup, tmp_path):
        self._fake(backup)
        path = make_backup(backup.module, tmp_path)
        backup.live = {KB_A: f"kb_{KB_A[:8]}"}
        assert backup.module.restore(path, True, parts=["files"], team="legal") == 0
        assert not backup.scripts

    def test_the_runner_passes_knowledge_only(self):
        runner = load("ops_runner")
        assert runner.restore_scope({"team": TEAM}) == ["--team", TEAM]
        assert runner.restore_scope({"team": TEAM, "knowledge_only": True}) == [
            "--team",
            TEAM,
            "--knowledge-only",
        ]
