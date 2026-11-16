"""Release hardening (E7): bundle signing, the SBOM and the ops runner's guards.

The scripts under ``scripts/`` are stdlib-only host tools, not part of the backend
package, so they are loaded by path. Signing tests need the ``openssl`` CLI and are
skipped without it. The offline installer uses the host's openssl too, so that is the
dependency being exercised.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import importlib.util
import json
import shutil
import sys
import threading
import urllib.parse
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"


def load(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not found")


def make_bundle(root: Path, name: str = "20261111T090000Z", **manifest) -> Path:
    bundle = root / name
    (bundle / "images").mkdir(parents=True)
    archive = bundle / "images" / "backend.tar"
    archive.write_bytes(b"image bytes")
    import hashlib

    body = {
        "format_version": 2,
        "created_at": name,
        "platform_version": "0.2.0",
        "images": {
            "backend": {
                "archive": "images/backend.tar",
                "sha256": hashlib.sha256(b"image bytes").hexdigest(),
            }
        },
        **manifest,
    }
    (bundle / "manifest.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    return bundle


@needs_openssl
class TestSigning:
    @pytest.fixture
    def signing(self):
        return load("bundle_signing")

    @pytest.fixture
    def keys(self, signing, tmp_path):
        return signing.generate_keypair(tmp_path / "keys")

    def test_round_trip(self, signing, keys, tmp_path):
        private, public = keys
        bundle = make_bundle(tmp_path)
        signing.sign_bundle(bundle, private)
        state, detail, signer = signing.bundle_signature_state(bundle, public)
        assert (state, detail) == ("valid", None)
        manifest = json.loads((bundle / "manifest.json").read_text())
        # Who signed is recorded inside what is signed.
        assert manifest["signing"]["key_fingerprint"] == signing.fingerprint(public) == signer
        assert manifest["signing"]["algorithm"] == "ecdsa-p256-sha256"

    def test_a_tampered_manifest_is_refused(self, signing, keys, tmp_path):
        private, public = keys
        bundle = make_bundle(tmp_path)
        signing.sign_bundle(bundle, private)
        path = bundle / "manifest.json"
        tampered = json.loads(path.read_text())
        tampered["images"]["backend"]["sha256"] = "0" * 64  # point at a different archive
        path.write_text(json.dumps(tampered, indent=2))
        assert signing.bundle_signature_state(bundle, public)[0] == "invalid"

    def test_the_wrong_key_is_refused(self, signing, keys, tmp_path):
        private, _public = keys
        _other_private, other_public = signing.generate_keypair(tmp_path / "other")
        bundle = make_bundle(tmp_path)
        signing.sign_bundle(bundle, private)
        state, detail, _signer = signing.bundle_signature_state(bundle, other_public)
        assert state == "invalid" and "different key" in detail

    def test_unsigned_and_untrusted_are_distinguished(self, signing, keys, tmp_path):
        private, public = keys
        unsigned = make_bundle(tmp_path, "20261111T000001Z")
        assert signing.bundle_signature_state(unsigned, public)[0] == "unsigned"
        signed = make_bundle(tmp_path, "20261111T000002Z")
        signing.sign_bundle(signed, private)
        assert signing.bundle_signature_state(signed, None)[0] == "untrusted"

    def test_keygen_refuses_to_overwrite_and_protects_the_private_key(self, signing, keys):
        private, _public = keys
        with pytest.raises(signing.SigningError):
            signing.generate_keypair(private.parent)
        if sys.platform != "win32":
            assert private.stat().st_mode & 0o077 == 0


class TestSbom:
    @pytest.fixture
    def sbom(self):
        return load("sbom")

    def test_lockfile_versions_and_hashes(self, sbom):
        packages = {
            p["name"]: p for p in sbom.parse_lockfile(REPO_ROOT / "backend" / "requirements.txt")
        }
        assert "fastapi" in packages and "sqlalchemy" in packages
        assert all(len(h) == 64 for p in packages.values() for h in p["hashes"])
        assert all(p["hashes"] for p in packages.values()), "every pinned package carries a hash"

    def test_document_lists_pinned_packages_images_and_vendor_js(self, sbom):
        document = sbom.build_sbom(platform_version="9.9.9", created_at="20261111T090000Z")
        assert document["bomFormat"] == "CycloneDX" and document["specVersion"] == "1.5"
        assert document["metadata"]["component"]["version"] == "9.9.9"
        assert document["metadata"]["timestamp"].startswith("2026-11-11T09:00:00")
        by_type: dict[str, list] = {}
        for component in document["components"]:
            by_type.setdefault(component["type"], []).append(component)

        fastapi = next(c for c in by_type["library"] if c["name"] == "fastapi")
        assert fastapi["purl"].startswith("pkg:pypi/fastapi@") and fastapi["hashes"]
        services = {p["value"] for p in fastapi["properties"] if p["name"] == "aip:service"}
        assert "backend" in services

        containers = {
            next(p["value"] for p in c["properties"] if p["name"] == "aip:service"): c
            for c in by_type["container"]
        }
        assert containers["postgres"]["version"].startswith("sha256:")
        assert "oidc-fixture" not in containers, "never-bundled services stay out of the SBOM"
        assert "${" not in containers["backend"]["version"]
        assert {"bootstrap", "chart.js"} <= {c["name"] for c in by_type["library"]}

    def test_a_bundle_build_passes_its_saved_images(self, sbom):
        document = sbom.build_sbom(
            platform_version="1.0",
            images={
                "backend": {
                    "image": "ai-platform/backend",
                    "digest": None,
                    "tag": "1.0",
                    "sha256": "a" * 64,
                }
            },
            lockfiles=(),
        )
        (container,) = [c for c in document["components"] if c["type"] == "container"]
        assert container["version"] == "sha256:" + "a" * 64
        assert container["hashes"] == [{"alg": "SHA-256", "content": "a" * 64}]

    def test_licenses_text(self, sbom, tmp_path):
        document = sbom.build_sbom(platform_version="1.0")
        sbom_path, licenses_path = sbom.write(tmp_path, document)
        assert json.loads(sbom_path.read_text())["components"]
        text = licenses_path.read_text()
        assert "## Libraries" in text and "## Container images" in text and "fastapi" in text


class TestRunnerGuards:
    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def site(self, runner, tmp_path):
        root = tmp_path / "site"
        (root / "backups" / "20261110T020000Z").mkdir(parents=True)
        (root / "backups" / "20261110T020000Z" / "manifest.json").write_text(
            json.dumps(
                {
                    "created_at": "20261110T020000Z",
                    "encryption_key_fingerprint": runner._key_fingerprint(
                        {"SECURITY__ENCRYPTION_KEY": "k1"}
                    ),
                    "artifacts": {"postgres.dump": {}},
                }
            )
        )
        (root / "backups" / "half-written").mkdir()
        make_bundle(root / "updates")
        (root / ".rollback" / "20261001T120000Z").mkdir(parents=True)
        return runner.Inventory(root, root / "updates", {"SECURITY__ENCRYPTION_KEY": "k1"})

    def test_inventory_reports_what_is_on_disk(self, site):
        report = site.report()
        backups = {b["name"]: b for b in report["backups"]}
        assert backups["20261110T020000Z"]["key_matches"] is True
        assert backups["20261110T020000Z"]["artifacts"] == ["postgres.dump"]
        assert backups["half-written"]["complete"] is False
        assert report["bundles"][0]["name"] == "20261111T090000Z"
        assert report["bundles"][0]["signature"] == "unsigned"
        assert report["rollback_points"][0]["name"] == "20261001T120000Z"
        assert report["disk"]["free_bytes"] > 0
        assert report["trust_key"]["configured"] is False

    def test_a_different_key_is_reported(self, runner, site):
        site.env = {"SECURITY__ENCRYPTION_KEY": "k2"}
        backups = {b["name"]: b for b in site.backups()}
        assert backups["20261110T020000Z"]["key_matches"] is False

    def test_only_listed_names_become_paths(self, runner, site):
        assert site.backup_path("20261110T020000Z").name == "20261110T020000Z"
        for bad in ("../etc", "20200101T000000Z", "", "a/b", ".hidden"):
            with pytest.raises(runner.JobError):
                site.backup_path(bad)
        with pytest.raises(runner.JobError):
            site.bundle_path("20261110T020000Z")  # a backup's name is not a bundle
        assert site.rollback_name("20261001T120000Z") == "20261001T120000Z"


class TestRunnerUpgrade:
    """E11: an upgrade migrates, then grants the release's new permissions to the roles."""

    class StubReporter:
        def __init__(self) -> None:
            self.result: dict = {}
            self.lines: list[str] = []

        def line(self, text: str) -> None:
            self.lines.append(text)

    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def site(self, runner, tmp_path):
        root = tmp_path / "site"
        (root / "offline").mkdir(parents=True)
        (root / "offline" / "upgrade.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        make_bundle(root / "updates")
        return runner.Inventory(root, root / "updates", {})

    def _run(self, runner, site, monkeypatch, fail_on: str | None = None):
        commands: list[list[str]] = []

        def fake_run(argv, reporter, *, cwd, env=None):
            commands.append([str(a) for a in argv])
            return 1 if fail_on and fail_on in argv else 0

        monkeypatch.setattr(runner, "run_command", fake_run)
        reporter = self.StubReporter()
        spec = {"kind": "upgrade", "params": {"bundle": "20261111T090000Z"}}
        outcome = runner.execute(spec, site, reporter, "python3")
        return outcome, commands, reporter

    def test_migrates_then_reconciles_rbac_without_touching_accounts(
        self, runner, site, monkeypatch
    ):
        outcome, commands, reporter = self._run(runner, site, monkeypatch)
        assert outcome == ("SUCCEEDED", None)
        assert commands[1][-3:] == ["alembic", "upgrade", "head"]
        assert commands[2][-5:] == ["python", "-m", "app.utils.cli", "seed", "--rbac-only"]
        assert reporter.result == {
            "bundle": "20261111T090000Z",
            "migrated": True,
            "rbac_reconciled": True,
        }

    def test_a_failed_reconcile_is_reported(self, runner, site, monkeypatch):
        (status, detail), _commands, _reporter = self._run(
            runner, site, monkeypatch, fail_on="--rbac-only"
        )
        assert status == "FAILED" and "seed --rbac-only" in detail


class TestRunnerPrune:
    """E12: retention deletes only listed, complete backups, and never the newest."""

    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def site(self, runner, tmp_path):
        root = tmp_path / "site"
        for name in ("20261101T000000Z", "20261102T000000Z", "20261103T000000Z"):
            (root / "backups" / name).mkdir(parents=True)
            (root / "backups" / name / "manifest.json").write_text("{}", encoding="utf-8")
        (root / "backups" / "20261104T000000Z").mkdir()  # still being written
        return runner.Inventory(root, root / "updates", {})

    def _prune(self, runner, site, names):
        reporter = TestRunnerUpgrade.StubReporter()
        spec = {"kind": "backup.prune", "params": {"delete": names}}
        return runner.execute(spec, site, reporter, "python3"), reporter

    def test_deletes_listed_old_backups(self, runner, site):
        outcome, reporter = self._prune(runner, site, ["20261101T000000Z"])
        assert outcome == ("SUCCEEDED", None)
        assert reporter.result["deleted"] == ["20261101T000000Z"]
        assert not (site.backups_dir / "20261101T000000Z").exists()

    def test_never_the_newest_complete_or_an_incomplete_one(self, runner, site):
        names = ["20261103T000000Z", "20261104T000000Z", "20261102T000000Z"]
        outcome, reporter = self._prune(runner, site, names)
        assert outcome == ("SUCCEEDED", None)
        assert reporter.result == {
            "deleted": ["20261102T000000Z"],
            "deleted_copies": [],
            "kept_newest": "20261103T000000Z",
        }
        assert (site.backups_dir / "20261103T000000Z").exists()
        assert (site.backups_dir / "20261104T000000Z").exists()

    def test_an_unlisted_name_fails_the_job(self, runner, site):
        with pytest.raises(runner.JobError):
            self._prune(runner, site, ["../etc"])


class TestRunnerCopy:
    """E23: off-host copies are complete or absent, and pruned with their originals."""

    NAMES = ("20261101T000000Z", "20261102T000000Z")

    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def site(self, runner, tmp_path):
        root = tmp_path / "site"
        for name in self.NAMES:
            backup = root / "backups" / name
            backup.mkdir(parents=True)
            (backup / "postgres.dump").write_bytes(b"dump-" + name.encode())
            (backup / "nested").mkdir()
            (backup / "nested" / "minio.tar").write_bytes(b"tar")
            (backup / "manifest.json").write_text("{}", encoding="utf-8")
        (tmp_path / "offsite").mkdir()
        env = {"OPS__BACKUP_COPY_DIR": str(tmp_path / "offsite")}
        return runner.Inventory(root, root / "updates", env)

    def _job(self, runner, site, kind, params):
        reporter = TestRunnerUpgrade.StubReporter()
        return runner.execute({"kind": kind, "params": params}, site, reporter, "python3"), reporter

    def test_inventory_reports_the_directory_and_what_is_copied(self, runner, site):
        assert site.copy_report()["configured"] is True
        assert site.copy_report()["reachable"] is True
        assert {b["copied"] for b in site.backups()} == {False}

    def test_a_copy_matches_the_original_file_for_file(self, runner, site):
        outcome, reporter = self._job(runner, site, "backup.copy", {"backup": self.NAMES[1]})
        assert outcome == ("SUCCEEDED", None)
        copy = site.copy_dir / self.NAMES[1]
        assert (copy / "nested" / "minio.tar").read_bytes() == b"tar"
        assert not any(p.name.startswith(".partial-") for p in site.copy_dir.iterdir())
        assert [b["copied"] for b in site.backups()] == [True, False]
        assert reporter.result == {"backup": self.NAMES[1], "copied": True}

    def test_a_copy_that_differs_is_removed_and_fails(self, runner, site, monkeypatch):
        real = runner._digests
        calls = []

        def corrupt(root):
            calls.append(root)
            digests = real(root)
            return {**digests, "postgres.dump": "0"} if len(calls) == 1 else digests

        monkeypatch.setattr(runner, "_digests", corrupt)
        (status, detail), _ = self._job(runner, site, "backup.copy", {"backup": self.NAMES[0]})
        assert status == "FAILED" and "does not match" in detail
        assert list(site.copy_dir.iterdir()) == []

    def test_an_unreachable_directory_fails_without_touching_the_backup(self, runner, site):
        site.copy_dir.rmdir()
        assert site.copy_report() == {
            "configured": True,
            "kind": "directory",
            "path": str(site.copy_dir),
            "reachable": False,
        }
        (status, detail), _ = self._job(runner, site, "backup.copy", {"backup": self.NAMES[0]})
        assert status == "FAILED" and "missing or not writable" in detail
        assert (site.backups_dir / self.NAMES[0] / "manifest.json").exists()

    def test_a_new_backup_is_copied_and_a_failed_copy_does_not_fail_it(
        self, runner, site, monkeypatch
    ):
        made = "20261103T000000Z"

        def fake_create(argv, reporter, *, cwd, env=None):
            backup = site.backups_dir / made
            backup.mkdir()
            (backup / "manifest.json").write_text("{}", encoding="utf-8")
            return 0

        monkeypatch.setattr(runner, "run_command", fake_create)
        monkeypatch.setattr(runner, "copy_backup", lambda *a: "disk full")
        outcome, reporter = self._job(runner, site, "backup.create", {})
        assert outcome == ("SUCCEEDED", None)
        assert reporter.result == {"backup": made, "copied": False, "copy_error": "disk full"}

    def test_retention_prunes_copies_too_but_never_the_newest(self, runner, site):
        for name in self.NAMES:
            self._job(runner, site, "backup.copy", {"backup": name})
        outcome, reporter = self._job(runner, site, "backup.prune", {"delete": list(self.NAMES)})
        assert outcome == ("SUCCEEDED", None)
        assert reporter.result["deleted_copies"] == [self.NAMES[0]]
        assert (site.copy_dir / self.NAMES[1]).exists()

    def test_no_directory_means_nothing_is_copied(self, runner, tmp_path):
        bare = runner.Inventory(tmp_path, tmp_path / "updates", {})
        assert bare.copy_report() == {"configured": False}
        assert bare.copied("20261101T000000Z") is None


class FakeS3(threading.Thread):
    """Just enough S3 for ``s3_copy`` (E31): path-style, one bucket, in memory.

    Like S3, it refuses a body whose SHA-256 is not the signed ``x-amz-content-sha256``,
    and a request signed with any access key but the expected one.
    """

    BUCKET = "backups"
    ACCESS = "AKTEST"
    NS = "http://s3.amazonaws.com/doc/2006-03-01/"

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.requests: list[tuple[str, str]] = []
        self.corrupt_next_put = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def route(self):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
                bucket, _, key = urllib.parse.unquote(url.path).lstrip("/").partition("/")
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append((self.command, key))
                if f"Credential={fake.ACCESS}/" not in (self.headers.get("Authorization") or ""):
                    return self.reply(403, b"<Error><Code>InvalidAccessKeyId</Code></Error>")
                if bucket != fake.BUCKET:
                    return self.reply(404, b"<Error><Code>NoSuchBucket</Code></Error>")
                if fake.corrupt_next_put and self.command == "PUT" and body:
                    fake.corrupt_next_put = False
                    body = body[:-1] + bytes([body[-1] ^ 1])
                if hashlib.sha256(body).hexdigest() != self.headers.get("x-amz-content-sha256"):
                    return self.reply(400, b"<Error><Code>XAmzContentSHA256Mismatch</Code></Error>")
                return fake.handle(self, key, query, body)

            do_HEAD = do_GET = do_PUT = do_POST = do_DELETE = route  # noqa: N815

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"

    def run(self) -> None:
        self.server.serve_forever()

    def handle(self, h, key, query, body):
        ns = self.NS
        if not key:
            if h.command == "HEAD":
                return h.reply(200)
            keys = sorted(k for k in self.objects if k.startswith(query.get("prefix", "")))
            start = int(query.get("continuation-token") or 0)  # two keys a page
            page, more = keys[start : start + 2], start + 2 < len(keys)
            xml = f'<ListBucketResult xmlns="{ns}">'
            xml += "".join(f"<Contents><Key>{k}</Key></Contents>" for k in page)
            xml += f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
            if more:
                xml += f"<NextContinuationToken>{start + 2}</NextContinuationToken>"
            return h.reply(200, (xml + "</ListBucketResult>").encode())
        if h.command == "POST" and "uploads" in query:
            upload_id = f"u{len(self.uploads)}"
            self.uploads[upload_id] = {}
            xml = f'<InitiateMultipartUploadResult xmlns="{ns}"><UploadId>{upload_id}</UploadId>'
            return h.reply(200, (xml + "</InitiateMultipartUploadResult>").encode())
        if h.command == "PUT" and "uploadId" in query:
            self.uploads[query["uploadId"]][int(query["partNumber"])] = body
            return h.reply(200, headers={"ETag": f'"{hashlib.sha256(body).hexdigest()[:32]}"'})
        if h.command == "POST" and "uploadId" in query:
            parts = self.uploads.pop(query["uploadId"])
            self.objects[key] = b"".join(parts[n] for n in sorted(parts))
            return h.reply(200, b"<CompleteMultipartUploadResult/>")
        if h.command == "PUT":
            self.objects[key] = body
            return h.reply(200)
        if h.command == "HEAD":
            return h.reply(200 if key in self.objects else 404)
        if h.command == "DELETE":
            self.objects.pop(key, None)
            return h.reply(204)
        return h.reply(405)


class TestRunnerS3Copy:
    """E31: off-host copies to S3-compatible storage, through the stdlib client."""

    NAMES = TestRunnerCopy.NAMES
    SECRET = "s3cr3t-never-shown"

    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def s3(self):
        fake = FakeS3()
        fake.start()
        yield fake
        fake.server.shutdown()
        fake.server.server_close()

    def _env(self, s3, **extra):
        return {
            "OPS__BACKUP_COPY_S3_ENDPOINT": s3.endpoint,
            "OPS__BACKUP_COPY_S3_BUCKET": FakeS3.BUCKET,
            "OPS__BACKUP_COPY_S3_ACCESS_KEY": FakeS3.ACCESS,
            "OPS__BACKUP_COPY_S3_SECRET_KEY": self.SECRET,
            "OPS__BACKUP_COPY_S3_PREFIX": "site-a",
            **extra,
        }

    def _site(self, runner, tmp_path, env):
        root = tmp_path / "site"
        for name in self.NAMES:
            backup = root / "backups" / name
            (backup / "nested").mkdir(parents=True)
            (backup / "postgres.dump").write_bytes(b"dump-" + name.encode())
            (backup / "nested" / "minio.tar").write_bytes(b"tar" * 10)
            (backup / "manifest.json").write_text("{}", encoding="utf-8")
        return runner.Inventory(root, root / "updates", env)

    def _job(self, runner, site, kind, params):
        reporter = TestRunnerUpgrade.StubReporter()
        return runner.execute({"kind": kind, "params": params}, site, reporter, "python3"), reporter

    def test_signature_matches_the_aws_worked_example(self):
        s3_copy = load("s3_copy")
        headers = s3_copy.sign(
            method="GET",
            host="examplebucket.s3.amazonaws.com",
            path="/test.txt",
            query={},
            headers={"Range": "bytes=0-9"},
            payload_sha256=s3_copy.EMPTY_SHA256,
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region="us-east-1",
            now=dt.datetime(2013, 5, 24, tzinfo=dt.UTC),
        )
        assert headers["authorization"].endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        )

    def test_a_copy_uploads_every_file_then_the_marker(self, runner, s3, tmp_path):
        site = self._site(runner, tmp_path, self._env(s3))
        report = site.copy_report()
        assert report["kind"] == "s3" and report["reachable"] is True
        assert self.SECRET not in json.dumps(report) and "free_bytes" not in report
        assert {b["copied"] for b in site.backups()} == {False}

        outcome, reporter = self._job(runner, site, "backup.copy", {"backup": self.NAMES[1]})
        assert outcome == ("SUCCEEDED", None)
        prefix = f"site-a/{self.NAMES[1]}/"
        assert s3.objects[prefix + "nested/minio.tar"] == b"tar" * 10
        puts = [k for m, k in s3.requests if m == "PUT"]
        assert puts[-1] == prefix + ".complete"  # last, so a partial copy never counts
        marker = json.loads(s3.objects[prefix + ".complete"])
        assert set(marker["files"]) == {"manifest.json", "postgres.dump", "nested/minio.tar"}
        assert [b["copied"] for b in site.backups()] == [True, False]
        assert not any(self.SECRET in line for line in reporter.lines)

    def test_a_file_that_arrives_different_is_refused_and_nothing_counts(
        self, runner, s3, tmp_path
    ):
        site = self._site(runner, tmp_path, self._env(s3))
        s3.corrupt_next_put = True
        (status, detail), _ = self._job(runner, site, "backup.copy", {"backup": self.NAMES[0]})
        assert status == "FAILED" and "XAmzContentSHA256Mismatch" in detail
        assert not any(k.endswith(".complete") for k in s3.objects)
        assert site.copied(self.NAMES[0]) is False

    def test_large_files_go_up_in_parts(self, runner, s3, tmp_path, monkeypatch):
        monkeypatch.setattr(load("s3_copy"), "PART_SIZE", 8)
        site = self._site(runner, tmp_path, self._env(s3))
        outcome, _ = self._job(runner, site, "backup.copy", {"backup": self.NAMES[0]})
        assert outcome == ("SUCCEEDED", None)
        assert s3.objects[f"site-a/{self.NAMES[0]}/nested/minio.tar"] == b"tar" * 10
        assert any(m == "POST" for m, _ in s3.requests)  # a multipart upload happened

    def test_wrong_keys_are_unreachable_and_fail_the_copy(self, runner, s3, tmp_path):
        site = self._site(runner, tmp_path, self._env(s3, OPS__BACKUP_COPY_S3_ACCESS_KEY="nobody"))
        assert site.copy_report()["reachable"] is False
        (status, detail), _ = self._job(runner, site, "backup.copy", {"backup": self.NAMES[0]})
        assert status == "FAILED" and "403" in detail

    def test_a_hanging_endpoint_does_not_stall_the_heartbeat(self, runner, tmp_path, monkeypatch):
        import socket
        import time

        silent = socket.socket()  # accepts connections, never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        try:
            monkeypatch.setattr(load("s3_copy"), "PROBE_TIMEOUT", 0.3)
            env = {
                "OPS__BACKUP_COPY_S3_ENDPOINT": f"http://127.0.0.1:{silent.getsockname()[1]}",
                "OPS__BACKUP_COPY_S3_BUCKET": FakeS3.BUCKET,
                "OPS__BACKUP_COPY_S3_ACCESS_KEY": FakeS3.ACCESS,
                "OPS__BACKUP_COPY_S3_SECRET_KEY": self.SECRET,
            }
            site = self._site(runner, tmp_path, env)
            started = time.monotonic()
            assert site.copy_report()["reachable"] is False
            assert {b["copied"] for b in site.backups()} == {False}
            assert time.monotonic() - started < 5
        finally:
            silent.close()

    def test_missing_settings_are_reported_not_guessed(self, runner, s3, tmp_path):
        env = self._env(s3, OPS__BACKUP_COPY_DIR=str(tmp_path))
        del env["OPS__BACKUP_COPY_S3_SECRET_KEY"]
        site = self._site(runner, tmp_path, env)
        report = site.copy_report()
        assert report["configured"] is True and report["reachable"] is False
        assert "OPS__BACKUP_COPY_S3_SECRET_KEY" in report["error"]
        assert site.copy_dir is None  # never silently falls back to the directory

    def test_retention_deletes_copies_but_never_the_newest(self, runner, s3, tmp_path):
        site = self._site(runner, tmp_path, self._env(s3))
        for name in self.NAMES:
            self._job(runner, site, "backup.copy", {"backup": name})
        outcome, reporter = self._job(runner, site, "backup.prune", {"delete": list(self.NAMES)})
        assert outcome == ("SUCCEEDED", None)
        assert reporter.result["deleted_copies"] == [self.NAMES[0]]
        assert not any(k.startswith(f"site-a/{self.NAMES[0]}/") for k in s3.objects)
        assert f"site-a/{self.NAMES[1]}/.complete" in s3.objects
