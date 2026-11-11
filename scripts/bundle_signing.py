#!/usr/bin/env python3
"""Sign and verify offline bundle manifests (E7, plan §5.16, threat model #9).

    python3 scripts/bundle_signing.py keygen --out ~/aip-signing          # once, on the build site
    python3 scripts/bundle_signing.py sign bundle/20261111T090000Z --key ~/aip-signing/bundle-signing.key
    python3 scripts/bundle_signing.py verify bundle/20261111T090000Z --trust-key bundle-signing.pub
    python3 scripts/bundle_signing.py fingerprint bundle-signing.pub

**What is signed.** ``manifest.json`` only. The manifest already carries the SHA-256 of every
image archive, every wheel set, every installer, every model file and the SBOM, and
``verify_bundle`` checks each against it. One signature over the manifest therefore covers
the whole bundle. Checksums alone prove a bundle is intact. The signature adds that it
came from whoever holds the key.

**Why ECDSA P-256 through the ``openssl`` CLI.** The offline installer verifies with the
*host's* ``openssl`` (``offline/lib.sh`` ``verify_signature``), never with code from
inside the bundle it is judging. P-256 with ``dgst -sha256`` works on every OpenSSL from
1.0.2 up. Ed25519 would need OpenSSL 3's ``pkeyutl -rawin``, which older LTS hosts lack.
This helper uses the same commands, so a signature it makes is one ``lib.sh`` accepts.

Stdlib only, like ``backup.py`` and ``build_bundle.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ALGORITHM = "ecdsa-p256-sha256"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
PRIVATE_NAME = "bundle-signing.key"
PUBLIC_NAME = "bundle-signing.pub"


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


def openssl_version() -> str | None:
    try:
        result = _run(["version"])
    except SigningError:
        return None
    return result.stdout.decode().strip() or None


def generate_keypair(directory: Path) -> tuple[Path, Path]:
    """Write ``bundle-signing.key`` (0600) and ``bundle-signing.pub`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
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


def public_from_private(private_key: Path) -> bytes:
    result = _run(["ec", "-in", str(private_key), "-pubout"])
    if result.returncode != 0:
        raise SigningError(f"{private_key} is not a readable EC private key.")
    return result.stdout


def private_fingerprint(private_key: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        pub = Path(tmp) / "pub.pem"
        pub.write_bytes(public_from_private(private_key))
        return fingerprint(pub)


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
    """``(state, detail, signer_fingerprint)`` where state is one of
    valid, invalid, unsigned, untrusted, error. Cheap: reads the manifest only."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    keygen = sub.add_parser("keygen", help="Create a signing key pair")
    keygen.add_argument("--out", type=Path, required=True)
    sign = sub.add_parser("sign", help="Sign a bundle's manifest")
    sign.add_argument("bundle", type=Path)
    sign.add_argument("--key", type=Path, required=True)
    verify = sub.add_parser("verify", help="Verify a bundle's signature")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--trust-key", type=Path, required=True)
    fp = sub.add_parser("fingerprint", help="Print a public key's fingerprint")
    fp.add_argument("public_key", type=Path)
    options = parser.parse_args()

    try:
        if options.command == "keygen":
            private, public = generate_keypair(options.out)
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
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
