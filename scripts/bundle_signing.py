#!/usr/bin/env python3
"""Sign and verify offline bundle manifests; rotate and revoke signing keys (E7, E63).

    python3 scripts/bundle_signing.py keygen --out ~/aip-signing          # once, on the build site
    python3 scripts/bundle_signing.py sign bundle/20261111T090000Z --key ~/aip-signing/bundle-signing.key
    python3 scripts/bundle_signing.py verify bundle/20261111T090000Z --trust-key bundle-signing.pub
    python3 scripts/bundle_signing.py fingerprint bundle-signing.pub

    # E63, on the build site: a trust update, shipped in every later bundle
    python3 scripts/bundle_signing.py statement --key ~/aip-signing/bundle-signing.key \\
        --add ~/aip-signing-2027/bundle-signing.pub=2027 --out offline/trust-updates
    # E63, on a site
    python3 scripts/bundle_signing.py trust list   --trust-dir /opt/ai-platform/.trust
    python3 scripts/bundle_signing.py trust add    new.pub --label 2027 --trust-dir ...
    python3 scripts/bundle_signing.py trust revoke sha256:... --reason "laptop stolen" --trust-dir ...
    python3 scripts/bundle_signing.py check  <bundle> --trust-dir ... [--apply]

    # E74: an offline root key, kept off the network, that signs trust updates only
    python3 scripts/bundle_signing.py keygen --out /media/offline-root --name root-signing
    python3 scripts/bundle_signing.py trust add root-signing.pub --root --label root --trust-dir ...
    python3 scripts/bundle_signing.py statement --key /media/offline-root/root-signing.key \
        --add ~/aip-signing-2027/bundle-signing.pub=2027 --expires 2028-01-31
    python3 scripts/bundle_signing.py statement --key /media/offline-root/root-signing.key \
        --renew sha256:...=2029-01-31

**What is signed.** ``manifest.json`` only. The manifest already carries the SHA-256 of every
image archive, every wheel set, every installer, every model file and the SBOM, and
``verify_bundle`` checks each against it. One signature over the manifest therefore covers
the whole bundle. Checksums alone prove a bundle is intact. The signature adds that it
came from whoever holds the key.

**Why ECDSA P-256 through the ``openssl`` CLI.** The offline installer verifies with the
*host's* ``openssl``, never with code from inside the bundle it is judging. P-256 with
``dgst -sha256`` works on every OpenSSL from 1.0.2 up. Ed25519 would need OpenSSL 3's
``pkeyutl -rawin``, which older LTS hosts lack.

**Several keys, and changing them (E63).** A site's trust store (``.trust/trust.json``) holds
any number of public keys. A bundle is accepted when its signature verifies against one that
is neither *retired* (a planned end) nor *revoked* (compromised). The single
``.trust/bundle-signing.pub`` of E7 still counts, as a key installed by hand.

Keys change through **trust updates**: small JSON statements, each signed by a key the site
already trusts, kept by the build site in ``offline/trust-updates/`` and therefore shipped in
every bundle's tree. A site applies, in sequence order, every statement it has not applied
yet, before checking the bundle's own signature. So a planned rotation is: generate the new
key, issue a statement signed by the old key that adds the new one, and sign the next bundle
with the new key. Every site that trusted the old key now trusts the new one, and nobody
carried a key file to it. A later statement retires the old key.

Rules that keep a statement from being worse than the key that signed it:

* Only an accepted key's statement is applied. A retired or revoked key signs nothing.
* A statement can't bring back a key this site has retired or revoked.
* **Revoking** a key also revokes every key that a trust update added on its authority, since
  whoever held a compromised key could have added their own. Retiring doesn't cascade.
* A statement that would leave the site with no accepted key is refused: a stolen key can
  deny service, but can't lock a site out by revoking every other key in one go.
* Adding a key by hand, or undoing a revocation, is only possible on the host (``trust add``,
  or editing ``trust.json``). The console can retire and revoke, never add.

**An offline root, and keys that expire (E74).** A key can be trusted as a **root**
(``trust add --root``). A root signs trust updates, never bundles, so it can live on a
machine that is never online. Once a site trusts a root, it takes trust updates from a
root only: whoever steals a signing key can sign bundles until it is revoked, but can't
add keys, keep the stolen one alive or revoke the others. A root key is only added by hand
or by another root's statement.

Any key can carry an **expiry date** (``--expires`` on ``trust add`` or on a statement's
``--add``). From the day after it, the key is *expired*: its bundles and statements are
refused as a retired key's are, until a statement (``--renew FINGERPRINT=DATE``) moves the
date. With a root, only a root renews; without one, any trusted key may (it could add a new
key anyway). The console and ``trust list`` show the days left.

Stdlib only, like ``backup.py`` and ``build_bundle.py``.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ALGORITHM = "ecdsa-p256-sha256"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
PRIVATE_NAME = "bundle-signing.key"
PUBLIC_NAME = "bundle-signing.pub"
#: E63: the site's trust store, inside its ``.trust/`` directory.
TRUST_STATE = "trust.json"
#: Where trust updates live in the repository, and so in a bundle's tree.
UPDATES_DIR = Path("offline") / "trust-updates"
STATEMENT_FORMAT = 1

#: Bundle states that refuse an install or upgrade.
REFUSED = frozenset({"invalid", "error", "retired", "revoked", "expired"})
#: E74: what a trusted key is for.
SIGNING, ROOT = "signing", "root"
#: E74: ``trust list`` and the console warn about keys expiring within this many days.
EXPIRY_WARNING_DAYS = 60


class SigningError(RuntimeError):
    pass


def _openssl() -> str:
    path = shutil.which("openssl")
    if not path:
        raise SigningError("openssl is required to sign or verify bundles and was not found.")
    return path


def _run(args: list[str], *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [_openssl(), *args], input=data, capture_output=True, check=False
    )


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def parse_date(value: str | None) -> str | None:
    """E74: an expiry date, ``YYYY-MM-DD``, checked. None or empty: no expiry."""
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError as exc:
        raise SigningError(f"{value!r} is not a date (YYYY-MM-DD).") from exc


def openssl_version() -> str | None:
    try:
        result = _run(["version"])
    except SigningError:
        return None
    return result.stdout.decode().strip() or None


def generate_keypair(directory: Path, name: str | None = None) -> tuple[Path, Path]:
    """Write ``bundle-signing.key`` (0600) and ``bundle-signing.pub`` into ``directory``
    (E74: ``<name>.key`` and ``<name>.pub``, e.g. for a root key kept beside it)."""
    directory.mkdir(parents=True, exist_ok=True)
    if name:
        private, public = directory / f"{name}.key", directory / f"{name}.pub"
    else:
        private, public = directory / PRIVATE_NAME, directory / PUBLIC_NAME
    if private.exists():
        raise SigningError(f"{private} already exists; refusing to overwrite a signing key.")
    old = os.umask(0o077)
    try:
        result = _run(["ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(private)])
    finally:
        os.umask(old)
    if result.returncode != 0:
        raise SigningError(result.stderr.decode().strip() or "key generation failed")
    private.chmod(0o600)
    result = _run(["ec", "-in", str(private), "-pubout", "-out", str(public)])
    if result.returncode != 0:
        raise SigningError(result.stderr.decode().strip() or "public key export failed")
    return private, public


def fingerprint(public_key: Path) -> str:
    """``sha256:<hex>`` of the DER-encoded public key: stable across PEM formatting."""
    result = _run(["pkey", "-pubin", "-in", str(public_key), "-outform", "DER"])
    if result.returncode != 0 or not result.stdout:
        raise SigningError(f"{public_key} is not a readable public key.")
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def _with_pem(pem: str):
    """A temporary file holding ``pem``, for the openssl CLI."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False, encoding="ascii")  # noqa: SIM115
    try:
        handle.write(pem)
    finally:
        handle.close()
    return Path(handle.name)


def pem_fingerprint(pem: str) -> str:
    path = _with_pem(pem)
    try:
        return fingerprint(path)
    finally:
        path.unlink(missing_ok=True)


def public_from_private(private_key: Path) -> bytes:
    result = _run(["ec", "-in", str(private_key), "-pubout"])
    if result.returncode != 0:
        raise SigningError(f"{private_key} is not a readable EC private key.")
    return result.stdout


def private_fingerprint(private_key: Path) -> str:
    return pem_fingerprint(public_from_private(private_key).decode("ascii"))


def sign_file(path: Path, private_key: Path, signature: Path) -> None:
    result = _run(["dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(path)])
    if result.returncode != 0:
        raise SigningError(result.stderr.decode().strip() or "signing failed")


def verify_file(path: Path, signature: Path, public_key: Path) -> bool:
    if not signature.exists():
        return False
    result = _run(
        ["dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature), str(path)]
    )
    return result.returncode == 0 and b"Verified OK" in result.stdout


def verify_with_pem(path: Path, signature: Path, pem: str) -> bool:
    key = _with_pem(pem)
    try:
        return verify_file(path, signature, key)
    finally:
        key.unlink(missing_ok=True)


def stamp_manifest(manifest: dict, private_key: Path) -> dict:
    """Record who signs, inside what is signed, so the claim is itself covered."""
    return {**manifest, "signing": {"algorithm": ALGORITHM, "key_fingerprint": private_fingerprint(private_key)}}


def sign_bundle(bundle: Path, private_key: Path) -> Path:
    manifest = bundle / MANIFEST
    if not manifest.exists():
        raise SigningError(f"{bundle} has no {MANIFEST}.")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    expected = private_fingerprint(private_key)
    recorded = (data.get("signing") or {}).get("key_fingerprint")
    if recorded != expected:
        data = stamp_manifest(data, private_key)
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    signature = bundle / SIGNATURE
    sign_file(manifest, private_key, signature)
    return signature


def bundle_signature_state(bundle: Path, trust_key: Path | None) -> tuple[str, str | None, str | None]:
    """Against one public key (E7). ``(state, detail, signer_fingerprint)`` where state is
    one of valid, invalid, unsigned, untrusted, error. Sites use :func:`check_bundle`."""
    manifest = bundle / MANIFEST
    signature = bundle / SIGNATURE
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "error", f"manifest unreadable: {type(exc).__name__}", None
    signer = (data.get("signing") or {}).get("key_fingerprint")
    if not signature.exists():
        return "unsigned", None, None
    if trust_key is None or not trust_key.exists():
        return "untrusted", f"signer {signer or 'unknown'}", signer
    try:
        trusted = fingerprint(trust_key)
        ok = verify_file(manifest, signature, trust_key)
    except SigningError as exc:
        return "error", str(exc), signer
    if not ok:
        detail = "signed by a different key" if signer and signer != trusted else "signature does not match the manifest"
        return "invalid", detail, signer
    return "valid", None, signer


# ---------------------------------------------------------------------------
# E63: the site's trust store
# ---------------------------------------------------------------------------
class TrustStore:
    """``.trust/trust.json``: every key a site has trusted, and what became of it.

    ``keys`` maps a fingerprint to its PEM, label, how it arrived (``installed`` by hand,
    or ``update`` with the fingerprint of the key whose statement ``added_by``) and when.
    ``retired`` and ``revoked`` map fingerprints to when, why and on whose authority
    (``local`` or a key). ``applied`` lists the trust updates applied, by content hash.
    """

    def __init__(self, directory: Path, state: dict[str, Any] | None = None) -> None:
        self.directory = directory
        self.state: dict[str, Any] = state or {
            "format": 1,
            "keys": {},
            "retired": {},
            "revoked": {},
            "applied": [],
        }

    @classmethod
    def load(cls, directory: Path) -> TrustStore:
        path = directory / TRUST_STATE
        state = None
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise SigningError(f"{path} is not valid JSON; fix or restore it.") from exc
        store = cls(directory, state)
        for field in ("keys", "retired", "revoked"):
            store.state.setdefault(field, {})
        store.state.setdefault("applied", [])
        legacy = directory / PUBLIC_NAME
        if legacy.exists():
            # The E7 single key: trusted as if installed by hand, unless retired or revoked.
            pem = legacy.read_text(encoding="ascii")
            fp = pem_fingerprint(pem)
            store.state["keys"].setdefault(
                fp, {"pem": pem, "label": "bundle-signing.pub", "source": "installed",
                     "added_by": None, "added_at": None}
            )
        return store

    def copy(self) -> TrustStore:
        return TrustStore(self.directory, copy.deepcopy(self.state))

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / TRUST_STATE
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    # -- reading ------------------------------------------------------------
    @property
    def configured(self) -> bool:
        """Any key ever trusted. From then on, unsigned bundles are refused."""
        return bool(self.state["keys"])

    def status(self, fp: str | None) -> str:
        if not fp or fp not in self.state["keys"]:
            return "unknown"
        if fp in self.state["revoked"]:
            return "revoked"
        if fp in self.state["retired"]:
            return "retired"
        expires = self.expires(fp)
        if expires is not None and dt.date.fromisoformat(expires) < _today():
            return "expired"
        return "trusted"

    def role(self, fp: str) -> str:
        """E74: ``root`` (signs trust updates only) or ``signing``."""
        return self.state["keys"].get(fp, {}).get("role") or SIGNING

    def expires(self, fp: str) -> str | None:
        return self.state["keys"].get(fp, {}).get("expires")

    def accepted(self) -> list[str]:
        return [fp for fp in self.state["keys"] if self.status(fp) == "trusted"]

    def signing_keys(self) -> list[str]:
        """Accepted keys that may sign bundles."""
        return [fp for fp in self.accepted() if self.role(fp) == SIGNING]

    @property
    def has_root(self) -> bool:
        """E74: a root this site hasn't retired or revoked (an expired one still counts:
        the site then waits for it to be renewed by hand rather than falling back)."""
        return any(
            self.role(fp) == ROOT and self.status(fp) in ("trusted", "expired")
            for fp in self.state["keys"]
        )

    def pem(self, fp: str) -> str:
        return self.state["keys"][fp]["pem"]

    def summary(self) -> dict[str, Any]:
        keys = []
        for fp, info in self.state["keys"].items():
            status = self.status(fp)
            ended = self.state["revoked"].get(fp) or self.state["retired"].get(fp) or {}
            expires = info.get("expires")
            days_left = (dt.date.fromisoformat(expires) - _today()).days if expires else None
            keys.append({
                "fingerprint": fp,
                "label": info.get("label"),
                "source": info.get("source"),
                "added_by": info.get("added_by"),
                "added_at": info.get("added_at"),
                "role": self.role(fp),
                "expires": expires,
                "days_left": days_left,
                "status": status,
                "ended_at": ended.get("at"),
                "reason": ended.get("reason"),
                "ended_by": ended.get("by"),
            })
        return {
            "configured": self.configured,
            "accepted": self.accepted(),
            "signing": self.signing_keys(),
            "root": self.has_root,
            "expiring": [
                k["fingerprint"]
                for k in keys
                if k["status"] == "trusted"
                and k["days_left"] is not None
                and k["days_left"] <= EXPIRY_WARNING_DAYS
            ],
            "keys": keys,
            "applied_updates": len(self.state["applied"]),
            "last_update": (self.state["applied"][-1] if self.state["applied"] else None),
        }

    # -- changing -----------------------------------------------------------
    def add(
        self,
        pem: str,
        *,
        label: str | None,
        source: str,
        added_by: str | None,
        role: str = SIGNING,
        expires: str | None = None,
    ) -> str:
        fp = pem_fingerprint(pem)
        if self.status(fp) in ("retired", "revoked"):
            raise SigningError(f"{fp} was {self.status(fp)} on this site and can't be trusted again this way.")
        if role not in (SIGNING, ROOT):
            raise SigningError(f"{role!r} is not a key role (signing or root).")
        self.state["keys"].setdefault(fp, {
            "pem": pem.strip() + "\n",
            "label": label,
            "source": source,
            "added_by": added_by,
            "added_at": _now(),
            "role": role,
            "expires": parse_date(expires),
        })
        return fp

    def renew(self, fp: str, expires: str | None) -> None:
        """E74: move a key's expiry date (None: never expires)."""
        if self.status(fp) not in ("trusted", "expired"):
            raise SigningError(f"{fp} is {self.status(fp)} here; only a trusted or expired key is renewed.")
        self.state["keys"][fp]["expires"] = parse_date(expires)

    def _end(self, field: str, fp: str, reason: str | None, by: str) -> None:
        if fp not in self.state["keys"]:
            raise SigningError(f"{fp} is not a key this site knows.")
        self.state[field].setdefault(fp, {"at": _now(), "reason": reason, "by": by})

    def retire(self, fp: str, *, reason: str | None, by: str) -> None:
        if self.status(fp) == "revoked":
            return
        self._end("retired", fp, reason, by)

    def revoke(self, fp: str, *, reason: str | None, by: str) -> list[str]:
        """Revoke ``fp`` and, transitively, every key a trust update added on its
        authority. Returns every fingerprint revoked."""
        revoked: list[str] = []
        pending = [(fp, reason)]
        while pending:
            current, why = pending.pop()
            if self.status(current) == "revoked":
                continue
            self._end("revoked", current, why, by)
            self.state["retired"].pop(current, None)
            revoked.append(current)
            for other, info in self.state["keys"].items():
                if info.get("source") == "update" and info.get("added_by") == current:
                    pending.append((other, f"added on the authority of revoked key {current}"))
        return revoked


# ---------------------------------------------------------------------------
# E63: trust updates
# ---------------------------------------------------------------------------
def statement_dir(bundle: Path) -> Path | None:
    """A bundle's trust updates: in its tree, or (a checkout) at the top."""
    for candidate in (bundle / "tree" / UPDATES_DIR, bundle / UPDATES_DIR):
        if candidate.is_dir():
            return candidate
    return None


def statements(directory: Path | None) -> list[tuple[int, Path, Path]]:
    """``(sequence, json, sig)`` in sequence order. Unreadable files are skipped here and
    reported by :func:`apply_statements`."""
    if directory is None or not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.json"):
        try:
            sequence = int(json.loads(path.read_text(encoding="utf-8")).get("sequence"))
        except (ValueError, TypeError, OSError):
            sequence = 0
        found.append((sequence, path, path.with_suffix(".sig")))
    return sorted(found, key=lambda item: (item[0], item[1].name))


def _pairs(values: Iterable[str]) -> list[tuple[str, str | None]]:
    out = []
    for value in values:
        head, _, tail = value.partition("=")
        out.append((head.strip(), tail.strip() or None))
    return out


def make_statement(
    out_dir: Path,
    private_key: Path,
    *,
    add: Iterable[tuple[Path, str | None]] = (),
    retire: Iterable[tuple[str, str | None]] = (),
    revoke: Iterable[tuple[str, str | None]] = (),
    renew: Iterable[tuple[str, str | None]] = (),
    expires: str | None = None,
    role: str = SIGNING,
) -> Path:
    """Write the next trust update into ``out_dir``, signed by ``private_key``. E74: keys
    it adds get ``role`` and ``expires``; ``renew`` moves existing keys' expiry dates."""
    additions = [
        {"fingerprint": fingerprint(path), "label": label, "public_key": path.read_text(encoding="ascii"),
         "role": role, "expires": parse_date(expires)}
        for path, label in add
    ]
    retirements = [{"fingerprint": fp, "reason": reason} for fp, reason in retire]
    revocations = [{"fingerprint": fp, "reason": reason} for fp, reason in revoke]
    renewals = [{"fingerprint": fp, "expires": parse_date(date)} for fp, date in renew]
    if not (additions or retirements or revocations or renewals):
        raise SigningError("A trust update needs at least one --add, --retire, --revoke or --renew.")
    out_dir.mkdir(parents=True, exist_ok=True)
    sequence = max([s for s, _j, _s in statements(out_dir)] or [0]) + 1
    body = {
        "format": STATEMENT_FORMAT,
        "sequence": sequence,
        "issued_at": _now(),
        "signed_by": private_fingerprint(private_key),
        "add": additions,
        "retire": retirements,
        "revoke": revocations,
        "renew": renewals,
    }
    path = out_dir / f"{sequence:04d}.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    sign_file(path, private_key, path.with_suffix(".sig"))
    return path


def apply_statements(store: TrustStore, directory: Path | None) -> list[dict[str, Any]]:
    """Apply, in order, every statement in ``directory`` the store hasn't applied. Changes
    ``store`` in memory; the caller saves it. Returns one outcome per new statement."""
    outcomes: list[dict[str, Any]] = []
    done = {entry.get("sha256") for entry in store.state["applied"]}
    for sequence, path, sig in statements(directory):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in done:
            continue
        outcome: dict[str, Any] = {"sequence": sequence, "file": path.name, "applied": False}
        outcomes.append(outcome)
        try:
            body = json.loads(raw)
            signer = body["signed_by"]
        except (ValueError, KeyError, TypeError):
            outcome["reason"] = "not a readable trust update"
            continue
        outcome["signed_by"] = signer
        status = store.status(signer)
        if status != "trusted":
            outcome["reason"] = (
                "signed by a key this site does not know" if status == "unknown"
                else f"signed by a {status} key"
            )
            continue
        if store.has_root and store.role(signer) != ROOT:
            # E74: with a root, a stolen signing key can't change what the site trusts.
            outcome["reason"] = "signed by a signing key; this site takes trust updates from its root key only"
            continue
        if not sig.exists() or not verify_with_pem(path, sig, store.pem(signer)):
            outcome["reason"] = "its signature does not verify"
            continue

        by_root = store.role(signer) == ROOT
        trial = store.copy()
        changes: dict[str, list[str]] = {
            "added": [], "retired": [], "revoked": [], "renewed": [], "skipped": [],
        }
        for item in body.get("add") or []:
            pem = item.get("public_key") or ""
            try:
                fp = pem_fingerprint(pem)
            except SigningError:
                changes["skipped"].append(f"{item.get('fingerprint')}: not a public key")
                continue
            if fp != item.get("fingerprint"):
                changes["skipped"].append(f"{item.get('fingerprint')}: fingerprint does not match the key")
                continue
            if trial.status(fp) in ("retired", "revoked"):
                changes["skipped"].append(f"{fp}: {trial.status(fp)} on this site")
                continue
            role = item.get("role") or SIGNING
            if role == ROOT and not by_root:
                changes["skipped"].append(f"{fp}: only a root key adds a root key")
                continue
            if fp not in trial.state["keys"]:
                try:
                    trial.add(pem, label=item.get("label"), source="update", added_by=signer,
                              role=role, expires=item.get("expires"))
                except SigningError as exc:
                    changes["skipped"].append(f"{fp}: {exc}")
                    continue
                changes["added"].append(fp)
        for item in body.get("retire") or []:
            fp = item.get("fingerprint")
            if trial.status(fp) == "trusted":
                trial.retire(fp, reason=item.get("reason"), by=signer)
                changes["retired"].append(fp)
        for item in body.get("revoke") or []:
            fp = item.get("fingerprint")
            if trial.status(fp) in ("trusted", "retired", "expired"):
                changes["revoked"].extend(trial.revoke(fp, reason=item.get("reason"), by=signer))
        for item in body.get("renew") or []:
            fp = item.get("fingerprint")
            if trial.status(fp) not in ("trusted", "expired"):
                changes["skipped"].append(f"{fp}: {trial.status(fp)}, not renewed")
                continue
            try:
                trial.renew(fp, item.get("expires"))
            except SigningError as exc:
                changes["skipped"].append(f"{fp}: {exc}")
                continue
            changes["renewed"].append(fp)
        if not trial.signing_keys():
            outcome["reason"] = "it would leave this site with no trusted key to sign bundles"
            continue
        trial.state["applied"].append({
            "sequence": sequence, "sha256": digest, "signed_by": signer, "applied_at": _now(),
        })
        store.state = trial.state
        done.add(digest)
        outcome.update(applied=True, **changes)
    return outcomes


def check_bundle(bundle: Path, trust_dir: Path, *, apply: bool = False) -> dict[str, Any]:
    """Judge a bundle against a site's trust store, after the trust updates it carries.

    ``state``: valid, invalid, unsigned, untrusted (no key configured), retired, revoked
    or error. With ``apply``, applied trust updates are saved, whether or not the bundle
    itself is then accepted: a revocation should take effect as soon as it arrives.
    """
    try:
        store = TrustStore.load(trust_dir)
    except SigningError as exc:
        return {"state": "error", "detail": str(exc), "signer": None, "updates": []}
    updates = apply_statements(store, statement_dir(bundle)) if store.configured else []
    if apply and any(u["applied"] for u in updates):
        store.save()
    result: dict[str, Any] = {"updates": updates, "signer": None, "detail": None}

    manifest, signature = bundle / MANIFEST, bundle / SIGNATURE
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {**result, "state": "error", "detail": f"manifest unreadable: {type(exc).__name__}"}
    signer = (data.get("signing") or {}).get("key_fingerprint")
    result["signer"] = signer
    if not signature.exists():
        return {**result, "state": "unsigned"}
    if not store.configured:
        return {**result, "state": "untrusted", "detail": f"signer {signer or 'unknown'}"}
    status = store.status(signer)
    try:
        if status in ("retired", "revoked"):
            ended = store.state[status][signer]
            why = f": {ended['reason']}" if ended.get("reason") else ""
            return {**result, "state": status, "detail": f"signed by a {status} key{why}"}
        if status == "expired":
            return {**result, "state": "expired",
                    "detail": f"signed by a key that expired on {store.expires(signer)}"}
        if status == "trusted" and store.role(signer) == ROOT:
            return {**result, "state": "invalid",
                    "detail": "signed by this site's root key, which signs trust updates only"}
        if status == "trusted":
            if verify_with_pem(manifest, signature, store.pem(signer)):
                return {**result, "state": "valid"}
            return {**result, "state": "invalid", "detail": "signature does not match the manifest"}
        # The manifest names no key, or one this site doesn't know: try every signing key.
        for fp in store.signing_keys():
            if verify_with_pem(manifest, signature, store.pem(fp)):
                return {**result, "state": "valid", "signer": fp}
    except SigningError as exc:
        return {**result, "state": "error", "detail": str(exc)}
    return {**result, "state": "invalid", "detail": f"signed by a key this site does not trust ({signer or 'unnamed'})"}


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _trust_command(options: argparse.Namespace) -> int:
    store = TrustStore.load(options.trust_dir)
    if options.trust_command == "list":
        print(json.dumps(store.summary(), indent=2))
        return 0
    if options.trust_command == "add":
        fp = store.add(options.public_key.read_text(encoding="ascii"), label=options.label,
                       source="installed", added_by=None,
                       role=ROOT if options.root else SIGNING, expires=options.expires)
        store.save()
        print(f"trusted{' as a root' if options.root else ''}: {fp}")
        return 0
    if options.trust_command == "renew":
        store.renew(options.fingerprint, options.expires)
        store.save()
        print(f"renewed: {options.fingerprint} ({options.expires or 'no expiry'})")
        return 0
    if options.trust_command == "apply":
        outcomes = apply_statements(store, statement_dir(options.bundle))
        store.save()
        for outcome in outcomes:
            print(json.dumps(outcome))
        return 0
    fp = options.fingerprint
    if options.trust_command in ("retire", "revoke"):
        trial = store.copy()
        if options.trust_command == "retire":
            trial.retire(fp, reason=options.reason, by="local")
            changed = [fp]
        else:
            changed = trial.revoke(fp, reason=options.reason, by="local")
        if not trial.signing_keys() and not options.force:
            raise SigningError(
                "That would leave this site with no trusted key, so every bundle would be "
                "refused. Add the replacement key first, or pass --force."
            )
        trial.save()
        print(f"{options.trust_command}d: {', '.join(changed)}")
        return 0
    raise SigningError(f"unknown trust command {options.trust_command!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="Create a signing key pair")
    keygen.add_argument("--out", type=Path, required=True)
    keygen.add_argument("--name", help="file name stem (default bundle-signing)")
    sign = sub.add_parser("sign", help="Sign a bundle's manifest")
    sign.add_argument("bundle", type=Path)
    sign.add_argument("--key", type=Path, required=True)
    verify = sub.add_parser("verify", help="Verify a bundle's signature against one key")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--trust-key", type=Path, required=True)
    fp = sub.add_parser("fingerprint", help="Print a public key's fingerprint")
    fp.add_argument("public_key", type=Path)

    statement = sub.add_parser("statement", help="Write the next trust update (build site)")
    statement.add_argument("--key", type=Path, required=True, help="private key of a key sites trust now")
    statement.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / UPDATES_DIR)
    statement.add_argument("--add", action="append", default=[], metavar="PUB[=LABEL]")
    statement.add_argument("--retire", action="append", default=[], metavar="FINGERPRINT[=REASON]")
    statement.add_argument("--revoke", action="append", default=[], metavar="FINGERPRINT[=REASON]")
    statement.add_argument("--renew", action="append", default=[], metavar="FINGERPRINT=DATE")
    statement.add_argument("--expires", metavar="DATE", help="expiry of the keys it adds")
    statement.add_argument("--root", action="store_true", help="the keys it adds are roots")

    check = sub.add_parser("check", help="Judge a bundle against a site's trust store")
    check.add_argument("bundle", type=Path)
    check.add_argument("--trust-dir", type=Path, required=True)
    check.add_argument("--apply", action="store_true", help="save the trust updates applied")

    trust = sub.add_parser("trust", help="Manage a site's trust store")
    trust.add_argument("--trust-dir", type=Path, required=True)
    tsub = trust.add_subparsers(dest="trust_command", required=True)
    tsub.add_parser("list")
    t_add = tsub.add_parser("add")
    t_add.add_argument("public_key", type=Path)
    t_add.add_argument("--label")
    t_add.add_argument("--root", action="store_true", help="signs trust updates, not bundles")
    t_add.add_argument("--expires", metavar="DATE")
    t_renew = tsub.add_parser("renew")
    t_renew.add_argument("fingerprint")
    t_renew.add_argument("--expires", metavar="DATE", help="omit for no expiry")
    t_apply = tsub.add_parser("apply")
    t_apply.add_argument("bundle", type=Path)
    for name in ("retire", "revoke"):
        t_end = tsub.add_parser(name)
        t_end.add_argument("fingerprint")
        t_end.add_argument("--reason")
        t_end.add_argument("--force", action="store_true")
    options = parser.parse_args()

    try:
        if options.command == "keygen":
            private, public = generate_keypair(options.out, options.name)
            print(f"private key: {private}  (keep it on the build site; never put it in a bundle)")
            print(f"public key:  {public}  (install on each site: install.sh --trust-key {public.name})")
            print(f"fingerprint: {fingerprint(public)}")
        elif options.command == "sign":
            print(f"signed: {sign_bundle(options.bundle, options.key)}")
        elif options.command == "verify":
            state, detail, signer = bundle_signature_state(options.bundle, options.trust_key)
            print(f"{state}{': ' + detail if detail else ''}")
            return 0 if state == "valid" else 1
        elif options.command == "fingerprint":
            print(fingerprint(options.public_key))
        elif options.command == "statement":
            adds = [(Path(p), label) for p, label in _pairs(options.add)]
            path = make_statement(
                options.out, options.key, add=adds,
                retire=_pairs(options.retire), revoke=_pairs(options.revoke),
                renew=_pairs(options.renew), expires=options.expires,
                role=ROOT if options.root else SIGNING,
            )
            print(f"trust update: {path}")
        elif options.command == "check":
            # Exit codes for offline/lib.sh: 0 accepted, 3 no trust configured (warn and
            # continue), 1 refused, 2 could not check.
            result = check_bundle(options.bundle, options.trust_dir, apply=options.apply)
            for update in result["updates"]:
                if update["applied"]:
                    parts = [f"{k} {len(update[k])}" for k in ("added", "retired", "revoked", "renewed")
                             if update.get(k)]
                    print(f"trust update {update['sequence']} applied ({', '.join(parts) or 'no change'})")
                else:
                    print(f"trust update {update['sequence']} ignored: {update['reason']}")
            state = result["state"]
            print(f"signature: {state}{' (' + result['detail'] + ')' if result['detail'] else ''}"
                  f"{' by ' + result['signer'] if result['signer'] and state == 'valid' else ''}")
            if state == "valid":
                return 0
            if state in ("untrusted", "unsigned") and not TrustStore.load(options.trust_dir).configured:
                return 3
            return 2 if state == "error" else 1
        elif options.command == "trust":
            return _trust_command(options)
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
