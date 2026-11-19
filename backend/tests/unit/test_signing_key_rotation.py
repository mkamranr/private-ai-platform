"""Bundle signing keys: several trusted at once, rotated and revoked (E63).

Real keys and signatures through the host's ``openssl``, as the installer uses: the trust
store, trust updates shipped in a bundle, the rules that keep a statement from being more
powerful than the key that signed it, and the ops runner's view and ``trust.change`` job.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.unit.test_release_tooling import load, make_bundle

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not found")


@pytest.fixture
def signing():
    return load("bundle_signing")


@pytest.fixture
def keys(signing, tmp_path):
    """Three key pairs: old, new and a stranger's."""
    return {
        name: signing.generate_keypair(tmp_path / "keys" / name) for name in ("old", "new", "evil")
    }


@pytest.fixture
def site(signing, keys, tmp_path) -> Path:
    """A site trusting only the old key, installed by hand."""
    trust = tmp_path / "site" / ".trust"
    store = signing.TrustStore.load(trust)
    store.add(keys["old"][1].read_text(), label="old", source="installed", added_by=None)
    store.save()
    return trust


def fp(signing, keys, name: str) -> str:
    return signing.fingerprint(keys[name][1])


def bundle_with(signing, tmp_path, signer: Path, name: str = "20261201T000000Z") -> Path:
    bundle = make_bundle(tmp_path / "bundles", name)
    signing.sign_bundle(bundle, signer)
    return bundle


def updates_dir(bundle: Path) -> Path:
    path = bundle / "tree" / "offline" / "trust-updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


class TestTrustStore:
    def test_any_accepted_key_verifies(self, signing, keys, site, tmp_path):
        store = signing.TrustStore.load(site)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.save()
        for i, name in enumerate(("old", "new")):
            bundle = bundle_with(signing, tmp_path, keys[name][0], f"2026120{i}T000000Z")
            result = signing.check_bundle(bundle, site)
            assert result["state"] == "valid" and result["signer"] == fp(signing, keys, name)

    def test_a_stranger_and_an_unsigned_bundle_are_refused(self, signing, keys, site, tmp_path):
        evil = signing.check_bundle(bundle_with(signing, tmp_path, keys["evil"][0]), site)
        assert evil["state"] == "invalid" and "does not trust" in evil["detail"]
        unsigned = make_bundle(tmp_path / "bundles", "20261202T000000Z")
        assert signing.check_bundle(unsigned, site)["state"] == "unsigned"

    def test_the_e7_single_key_still_counts(self, signing, keys, tmp_path):
        trust = tmp_path / "legacy" / ".trust"
        trust.mkdir(parents=True)
        shutil.copy(keys["old"][1], trust / signing.PUBLIC_NAME)
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        assert signing.check_bundle(bundle, trust)["state"] == "valid"
        summary = signing.TrustStore.load(trust).summary()
        assert summary["configured"] and summary["keys"][0]["source"] == "installed"

    def test_no_store_is_untrusted_not_refused(self, signing, keys, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        assert signing.check_bundle(bundle, tmp_path / "nothing")["state"] == "untrusted"

    def test_retired_and_revoked_keys_are_refused(self, signing, keys, site, tmp_path):
        store = signing.TrustStore.load(site)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.retire(fp(signing, keys, "old"), reason="rotated", by="local")
        store.save()
        retired = signing.check_bundle(bundle_with(signing, tmp_path, keys["old"][0]), site)
        assert retired["state"] == "retired" and "rotated" in retired["detail"]
        store.revoke(fp(signing, keys, "old"), reason="leaked", by="local")
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["old"][0], "20261203T000000Z")
        assert signing.check_bundle(bundle, site)["state"] == "revoked"
        # A revoked key can't simply be added back.
        with pytest.raises(signing.SigningError):
            store.add(keys["old"][1].read_text(), label="again", source="installed", added_by=None)


class TestTrustUpdates:
    def test_a_rotation_reaches_the_site_inside_the_bundle(self, signing, keys, site, tmp_path):
        # Signed by the NEW key; the statement adding it is signed by the OLD one.
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        assert signing.check_bundle(bundle, site)["state"] == "invalid"
        signing.make_statement(updates_dir(bundle), keys["old"][0], add=[(keys["new"][1], "2027")])

        preview = signing.check_bundle(bundle, site)
        assert preview["state"] == "valid"
        assert preview["updates"][0]["applied"] and preview["updates"][0]["added"] == [
            fp(signing, keys, "new")
        ]
        # Without --apply nothing was saved.
        assert fp(signing, keys, "new") not in signing.TrustStore.load(site).accepted()

        applied = signing.check_bundle(bundle, site, apply=True)
        assert applied["state"] == "valid"
        store = signing.TrustStore.load(site)
        info = store.state["keys"][fp(signing, keys, "new")]
        assert (info["source"], info["added_by"], info["label"]) == (
            "update",
            fp(signing, keys, "old"),
            "2027",
        )
        # Applied once: the next check finds nothing new.
        assert signing.check_bundle(bundle, site, apply=True)["updates"] == []

    def test_a_chain_is_applied_in_order(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        directory = updates_dir(bundle)
        signing.make_statement(directory, keys["old"][0], add=[(keys["new"][1], "2027")])
        # The second statement is signed by the key the first one added, and retires the old.
        signing.make_statement(
            directory, keys["new"][0], retire=[(fp(signing, keys, "old"), "rotated")]
        )
        result = signing.check_bundle(bundle, site, apply=True)
        assert [u["sequence"] for u in result["updates"]] == [1, 2]
        assert all(u["applied"] for u in result["updates"])
        store = signing.TrustStore.load(site)
        assert store.status(fp(signing, keys, "old")) == "retired"
        assert store.accepted() == [fp(signing, keys, "new")]
        assert result["state"] == "valid"

    def test_a_statement_from_a_stranger_is_ignored(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["evil"][0])
        signing.make_statement(updates_dir(bundle), keys["evil"][0], add=[(keys["evil"][1], "me")])
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["updates"][0]["applied"] is False
        assert "does not know" in result["updates"][0]["reason"]
        assert result["state"] == "invalid"

    def test_a_tampered_statement_is_ignored(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["evil"][0])
        path = signing.make_statement(
            updates_dir(bundle), keys["old"][0], add=[(keys["new"][1], "2027")]
        )
        body = json.loads(path.read_text())
        evil_pem = keys["evil"][1].read_text()
        body["add"] = [
            {"fingerprint": fp(signing, keys, "evil"), "label": "x", "public_key": evil_pem}
        ]
        path.write_text(json.dumps(body))
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["updates"][0]["reason"] == "its signature does not verify"
        assert result["state"] == "invalid"

    def test_a_key_that_does_not_match_its_fingerprint_is_skipped(
        self, signing, keys, site, tmp_path
    ):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        directory = updates_dir(bundle)
        path = directory / "0001.json"
        body = {
            "format": 1,
            "sequence": 1,
            "signed_by": fp(signing, keys, "old"),
            "add": [
                {"fingerprint": fp(signing, keys, "new"), "public_key": keys["evil"][1].read_text()}
            ],
        }
        path.write_text(json.dumps(body))
        signing.sign_file(path, keys["old"][0], path.with_suffix(".sig"))
        update = signing.check_bundle(bundle, site, apply=True)["updates"][0]
        assert update["applied"] and update["added"] == []
        assert "does not match" in update["skipped"][0]

    def test_revoking_cascades_to_keys_it_added(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        signing.make_statement(
            updates_dir(bundle), keys["old"][0], add=[(keys["new"][1], "added by old")]
        )
        signing.check_bundle(bundle, site, apply=True)
        store = signing.TrustStore.load(site)
        store.add(keys["evil"][1].read_text(), label="by hand", source="installed", added_by=None)
        revoked = store.revoke(fp(signing, keys, "old"), reason="stolen", by="local")
        assert set(revoked) == {fp(signing, keys, "old"), fp(signing, keys, "new")}
        # Keys installed by hand don't depend on it.
        assert store.accepted() == [fp(signing, keys, "evil")]
        assert "revoked key" in store.state["revoked"][fp(signing, keys, "new")]["reason"]

    def test_retiring_does_not_cascade(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        signing.make_statement(updates_dir(bundle), keys["old"][0], add=[(keys["new"][1], "2027")])
        signing.check_bundle(bundle, site, apply=True)
        store = signing.TrustStore.load(site)
        store.retire(fp(signing, keys, "old"), reason="rotated", by="local")
        assert store.accepted() == [fp(signing, keys, "new")]

    def test_a_statement_cannot_leave_the_site_with_no_key(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        signing.make_statement(
            updates_dir(bundle), keys["old"][0], revoke=[(fp(signing, keys, "old"), "all gone")]
        )
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["updates"][0]["applied"] is False
        assert "no trusted key" in result["updates"][0]["reason"]
        assert signing.TrustStore.load(site).accepted() == [fp(signing, keys, "old")]
        assert result["state"] == "valid"

    def test_a_revoked_key_signs_no_statements(self, signing, keys, site, tmp_path):
        store = signing.TrustStore.load(site)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.revoke(fp(signing, keys, "old"), reason="stolen", by="local")
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        signing.make_statement(
            updates_dir(bundle), keys["old"][0], add=[(keys["evil"][1], "sneaky")]
        )
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["updates"][0]["reason"] == "signed by a revoked key"
        assert fp(signing, keys, "evil") not in signing.TrustStore.load(site).state["keys"]

    def test_a_statement_cannot_restore_a_revoked_key(self, signing, keys, site, tmp_path):
        store = signing.TrustStore.load(site)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.revoke(fp(signing, keys, "old"), reason="stolen", by="local")
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        signing.make_statement(updates_dir(bundle), keys["new"][0], add=[(keys["old"][1], "back")])
        update = signing.check_bundle(bundle, site, apply=True)["updates"][0]
        assert update["added"] == [] and "revoked on this site" in update["skipped"][0]

    def test_sequences_increase(self, signing, keys, tmp_path):
        directory = tmp_path / "updates"
        first = signing.make_statement(directory, keys["old"][0], add=[(keys["new"][1], None)])
        second = signing.make_statement(
            directory, keys["old"][0], retire=[(fp(signing, keys, "old"), None)]
        )
        assert (first.name, second.name) == ("0001.json", "0002.json")
        with pytest.raises(signing.SigningError):
            signing.make_statement(directory, keys["old"][0])


class TestRunner:
    @pytest.fixture
    def runner(self):
        return load("ops_runner")

    @pytest.fixture
    def inventory(self, runner, signing, keys, site, tmp_path):
        root = site.parent
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        shutil.copytree(bundle, root / "updates" / bundle.name)
        signing.make_statement(
            updates_dir(root / "updates" / bundle.name),
            keys["old"][0],
            add=[(keys["new"][1], "2027")],
        )
        return runner.Inventory(root, root / "updates", {})

    class StubReporter:
        def __init__(self) -> None:
            self.result: dict = {}
            self.lines: list[str] = []

        def line(self, text: str) -> None:
            self.lines.append(text)

    def test_reports_keys_and_judges_bundles_after_their_updates(self, signing, keys, inventory):
        report = inventory.report()
        trust = report["trust_key"]
        assert trust["configured"] and trust["fingerprint"] == fp(signing, keys, "old")
        assert [k["status"] for k in trust["keys"]] == ["trusted"]
        bundle = report["bundles"][0]
        assert bundle["signature"] == "valid"
        assert bundle["trust_updates"][0]["applied"] is True
        # Listing changed nothing on the host.
        assert signing.TrustStore.load(inventory.trust_dir).accepted() == [fp(signing, keys, "old")]

    def test_trust_change_retires_and_revokes(self, runner, signing, keys, inventory):
        store = signing.TrustStore.load(inventory.trust_dir)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.save()
        old = fp(signing, keys, "old")
        spec = {
            "kind": "trust.change",
            "params": {"fingerprint": old, "action": "retire", "reason": "rotated"},
        }
        assert runner.execute(spec, inventory, self.StubReporter(), "python3") == (
            "SUCCEEDED",
            None,
        )
        assert signing.TrustStore.load(inventory.trust_dir).status(old) == "retired"
        spec["params"]["action"] = "revoke"
        assert runner.execute(spec, inventory, self.StubReporter(), "python3") == (
            "SUCCEEDED",
            None,
        )
        assert signing.TrustStore.load(inventory.trust_dir).status(old) == "revoked"

    def test_trust_change_refuses_the_last_key_and_bad_input(
        self, runner, signing, keys, inventory
    ):
        old = fp(signing, keys, "old")
        last = {"kind": "trust.change", "params": {"fingerprint": old, "action": "revoke"}}
        status, message = runner.execute(last, inventory, self.StubReporter(), "python3")
        assert status == "FAILED" and "no trusted key" in message
        for params in (
            {"fingerprint": "nope", "action": "revoke"},
            {"fingerprint": old, "action": "add"},
            {"fingerprint": "sha256:" + "0" * 64, "action": "retire"},
        ):
            spec = {"kind": "trust.change", "params": params}
            assert runner.execute(spec, inventory, self.StubReporter(), "python3")[0] == "FAILED"
        assert signing.TrustStore.load(inventory.trust_dir).accepted() == [old]

    def test_verify_job_refuses_a_revoked_signer(
        self, runner, signing, keys, inventory, monkeypatch
    ):
        store = signing.TrustStore.load(inventory.trust_dir)
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.add(keys["evil"][1].read_text(), label="spare", source="installed", added_by=None)
        store.revoke(fp(signing, keys, "new"), reason="leaked", by="local")
        store.save()
        monkeypatch.setattr(runner, "run_command", lambda *a, **k: 0)
        name = inventory.bundles()[0]["name"]
        spec = {"kind": "bundle.verify", "params": {"bundle": name}}
        status, message = runner.execute(spec, inventory, self.StubReporter(), "python3")
        assert status == "FAILED" and "revoked" in message
