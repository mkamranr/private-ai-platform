"""Release hardening (E7): bundle signing, the SBOM and the ops runner's guards.

The scripts under ``scripts/`` are stdlib-only host tools, not part of the backend
package, so they are loaded by path. Signing tests need the ``openssl`` CLI and are
skipped without it. The offline installer uses the host's openssl too, so that is the
dependency being exercised.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
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
            "kept_newest": "20261103T000000Z",
        }
        assert (site.backups_dir / "20261103T000000Z").exists()
        assert (site.backups_dir / "20261104T000000Z").exists()

    def test_an_unlisted_name_fails_the_job(self, runner, site):
        with pytest.raises(runner.JobError):
            self._prune(runner, site, ["../etc"])
