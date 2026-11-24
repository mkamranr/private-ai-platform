"""Bundle signing: an offline root key, and keys that expire (E74).

Real keys and signatures through the host's ``openssl``, as in the E63 tests.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from tests.unit.test_release_tooling import load
from tests.unit.test_signing_key_rotation import bundle_with, updates_dir

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not found")


@pytest.fixture
def signing():
    return load("bundle_signing")


@pytest.fixture
def keys(signing, tmp_path):
    """A root, the current signing key, its successor and a stranger's."""
    return {
        name: signing.generate_keypair(tmp_path / "keys" / name)
        for name in ("root", "old", "new", "evil")
    }


def fp(signing, keys, name: str) -> str:
    return signing.fingerprint(keys[name][1])


def day(offset: int) -> str:
    return (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=offset)).isoformat()


@pytest.fixture
def site(signing, keys, tmp_path) -> Path:
    """A site trusting a root and the old signing key, both installed by hand."""
    trust = tmp_path / "site" / ".trust"
    store = signing.TrustStore.load(trust)
    store.add(
        keys["root"][1].read_text(), label="root", source="installed", added_by=None, role="root"
    )
    store.add(keys["old"][1].read_text(), label="old", source="installed", added_by=None)
    store.save()
    return trust


def statement(signing, bundle: Path, signer: Path, **kwargs) -> Path:
    return signing.make_statement(updates_dir(bundle), signer, **kwargs)


class TestRoot:
    def test_a_root_signs_no_bundles(self, signing, keys, site, tmp_path):
        result = signing.check_bundle(bundle_with(signing, tmp_path, keys["root"][0]), site)
        assert result["state"] == "invalid" and "root key" in result["detail"]

    def test_the_root_adds_a_signing_key(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        statement(
            signing, bundle, keys["root"][0], add=[(keys["new"][1], "2027")], expires=day(400)
        )
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["state"] == "valid", result
        store = signing.TrustStore.load(site)
        assert store.role(fp(signing, keys, "new")) == "signing"
        assert store.expires(fp(signing, keys, "new")) == day(400)

    def test_with_a_root_a_signing_keys_statements_are_ignored(self, signing, keys, site, tmp_path):
        # A stolen signing key tries to add its holder's key and revoke the others.
        bundle = bundle_with(signing, tmp_path, keys["evil"][0])
        statement(signing, bundle, keys["old"][0], add=[(keys["evil"][1], "mine")])
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["state"] == "invalid"
        (update,) = result["updates"]
        assert not update["applied"] and "root key only" in update["reason"]
        assert signing.TrustStore.load(site).status(fp(signing, keys, "evil")) == "unknown"

    def test_without_a_root_signing_keys_still_rotate(self, signing, keys, tmp_path):
        trust = tmp_path / "plain" / ".trust"
        store = signing.TrustStore.load(trust)
        store.add(keys["old"][1].read_text(), label="old", source="installed", added_by=None)
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["new"][0])
        statement(signing, bundle, keys["old"][0], add=[(keys["new"][1], "2027")])
        assert signing.check_bundle(bundle, trust)["state"] == "valid"

    def test_only_a_root_adds_a_root(self, signing, keys, tmp_path):
        trust = tmp_path / "plain" / ".trust"
        store = signing.TrustStore.load(trust)
        store.add(keys["old"][1].read_text(), label="old", source="installed", added_by=None)
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        statement(signing, bundle, keys["old"][0], add=[(keys["evil"][1], "root?")], role="root")
        (update,) = signing.check_bundle(bundle, trust, apply=True)["updates"]
        assert update["applied"] and update["added"] == []
        assert "only a root key adds a root key" in update["skipped"][0]

    def test_the_root_cannot_leave_no_signing_key(self, signing, keys, site, tmp_path):
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        statement(signing, bundle, keys["root"][0], revoke=[(fp(signing, keys, "old"), "oops")])
        result = signing.check_bundle(bundle, site, apply=True)
        assert "no trusted key" in result["updates"][0]["reason"]
        assert result["state"] == "valid"


class TestExpiry:
    def test_an_expired_key_signs_nothing(self, signing, keys, tmp_path):
        trust = tmp_path / "exp" / ".trust"
        store = signing.TrustStore.load(trust)
        store.add(
            keys["old"][1].read_text(),
            label="old",
            source="installed",
            added_by=None,
            expires=day(-1),
        )
        store.add(
            keys["new"][1].read_text(),
            label="new",
            source="installed",
            added_by=None,
            expires=day(0),
        )
        store.save()
        old = signing.check_bundle(
            bundle_with(signing, tmp_path, keys["old"][0], "20261201T000000Z"), trust
        )
        assert old["state"] == "expired" and day(-1) in old["detail"]
        # The last day still counts.
        new = signing.check_bundle(
            bundle_with(signing, tmp_path, keys["new"][0], "20261202T000000Z"), trust
        )
        assert new["state"] == "valid"
        summary = signing.TrustStore.load(trust).summary()
        assert summary["expiring"] == [fp(signing, keys, "new")]
        days = {k["fingerprint"]: k["days_left"] for k in summary["keys"]}
        assert days[fp(signing, keys, "old")] == -1 and days[fp(signing, keys, "new")] == 0

    def test_the_root_renews_a_key(self, signing, keys, site, tmp_path):
        store = signing.TrustStore.load(site)
        store.renew(fp(signing, keys, "old"), day(-3))
        store.add(keys["new"][1].read_text(), label="new", source="installed", added_by=None)
        store.save()
        bundle = bundle_with(signing, tmp_path, keys["old"][0])
        assert signing.check_bundle(bundle, site)["state"] == "expired"
        statement(signing, bundle, keys["root"][0], renew=[(fp(signing, keys, "old"), day(30))])
        result = signing.check_bundle(bundle, site, apply=True)
        assert result["state"] == "valid", result
        assert result["updates"][0]["renewed"] == [fp(signing, keys, "old")]

    def test_dates_are_checked(self, signing, keys, site):
        store = signing.TrustStore.load(site)
        with pytest.raises(signing.SigningError, match="not a date"):
            store.renew(fp(signing, keys, "old"), "next tuesday")

    def test_the_command_line(self, signing, keys, tmp_path, monkeypatch):
        trust = tmp_path / "cli" / ".trust"
        run = lambda *args: signing_main(signing, monkeypatch, *args)  # noqa: E731
        assert run("trust", "--trust-dir", str(trust), "add", str(keys["root"][1]), "--root") == 0
        assert (
            run(
                "trust", "--trust-dir", str(trust), "add", str(keys["old"][1]), "--expires", day(10)
            )
            == 0
        )
        assert (
            run(
                "trust",
                "--trust-dir",
                str(trust),
                "renew",
                fp(signing, keys, "old"),
                "--expires",
                day(90),
            )
            == 0
        )
        store = signing.TrustStore.load(trust)
        assert store.role(fp(signing, keys, "root")) == "root"
        assert store.expires(fp(signing, keys, "old")) == day(90)
        out = tmp_path / "updates"
        assert (
            run(
                "statement",
                "--key",
                str(keys["root"][0]),
                "--out",
                str(out),
                "--add",
                f"{keys['new'][1]}=2027",
                "--expires",
                day(365),
            )
            == 0
        )
        body = (out / "0001.json").read_text()
        assert day(365) in body and '"role": "signing"' in body


def signing_main(signing, monkeypatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["bundle_signing.py", *args])
    return signing.main()
