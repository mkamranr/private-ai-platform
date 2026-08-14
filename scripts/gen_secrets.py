#!/usr/bin/env python3
"""Fill a fresh .env with real secrets (M02).

`.env.example` ships `change-me-...` placeholders so the file documents what each setting
is. They are not merely weak, they are **invalid**: `SECURITY__ENCRYPTION_KEY` has to be a
32-byte url-safe base64 Fernet key, and the placeholder is rejected outright with
`ValueError: Fernet key must be 32 url-safe base64-encoded bytes`. A first-time reader who
copies the template and runs `make up` therefore gets a backend that will not start, for a
reason that has nothing to do with anything they did.

So this generates them. Run by the Makefile's `.env` target on first use, and safe to run
again: **only placeholder values are replaced**, so an operator's own secrets are never
overwritten by someone running `make` in the wrong directory.

Not a substitute for a site's own key management. It gets a developer running in one
command; production secrets belong wherever that site keeps its secrets.
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
from pathlib import Path

#: Setting -> how to make one. Each is the shape that setting actually requires, which is
#: the whole point: a "random string" in the Fernet slot fails at startup.
GENERATORS: dict[str, callable] = {
    # 32 url-safe base64 bytes. Fernet validates this on construction.
    "SECURITY__ENCRYPTION_KEY": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "AUTH__JWT_SECRET_KEY": lambda: secrets.token_hex(32),
    "NODE_AGENT_AUTH_TOKEN": lambda: secrets.token_hex(32),
    "LANGFUSE__NEXTAUTH_SECRET": lambda: secrets.token_hex(32),
    "LANGFUSE__SALT": lambda: secrets.token_hex(32),
    # Passwords, not keys: URL-safe so they survive a DSN without escaping. MinIO refuses
    # anything under 8 characters.
    "DATABASE__PASSWORD": lambda: secrets.token_urlsafe(24),
    "MINIO__SECRET_KEY": lambda: secrets.token_urlsafe(24),
    "AUTH__BOOTSTRAP_ADMIN_PASSWORD": lambda: secrets.token_urlsafe(18),
    "GRAFANA__ADMIN_PASSWORD": lambda: secrets.token_urlsafe(18),
}

#: A value counts as unset if it still looks like the template's.
PLACEHOLDER_MARKERS = ("change-me", "CHANGE ME", "change_me")


def is_placeholder(value: str) -> bool:
    return not value.strip() or any(marker in value for marker in PLACEHOLDER_MARKERS)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even settings that already hold a real value. Destructive: "
        "rotating SECURITY__ENCRYPTION_KEY makes every stored credential undecryptable.",
    )
    args = parser.parse_args(argv)

    path = Path(args.env_file)
    if not path.is_file():
        print(f"{path} does not exist. Copy .env.example to it first.", file=sys.stderr)
        return 1

    lines = path.read_text().splitlines(keepends=True)
    filled: list[str] = []
    kept: list[str] = []

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name not in GENERATORS:
            continue
        if not args.force and not is_placeholder(value):
            kept.append(name)
            continue
        lines[index] = f"{name}={GENERATORS[name]()}\n"
        filled.append(name)

    if filled:
        # Written 0600 before the content lands in it: this file holds every credential the
        # platform has, and a world-readable moment is still a moment.
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text("".join(lines))

    if filled:
        print(f"Generated {len(filled)} secret(s) in {path}: {', '.join(sorted(filled))}")
    if kept:
        print(f"Left {len(kept)} existing value(s) alone: {', '.join(sorted(kept))}")
    if not filled and not kept:
        print(f"No known secret settings found in {path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
