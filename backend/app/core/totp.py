"""Time-based one-time passwords (RFC 6238) and recovery codes (expansion E15).

Standard library only. The parameters are the ones every authenticator app accepts
without asking: HMAC-SHA1, 6 digits, a 30-second step. SHA-1 is not a weakness here;
HOTP's security rests on HMAC, not on SHA-1's collision resistance.

**Replay.** A code is valid for its step and one step either side, to absorb clock drift
between the phone and an air-gapped host. Within that window a code that has already been
accepted must not be accepted again. :func:`verify` therefore returns the step it matched,
the caller stores it, and the next verification refuses that step and any earlier one.

**Recovery codes** are single-use and stored as SHA-256 hashes. They carry 50 bits of
entropy and every wrong guess counts toward the account lockout, so a slow hash adds nothing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
STEP_SECONDS = 30
#: Steps accepted either side of now: 30 s of drift each way.
WINDOW = 1
SECRET_BYTES = 20  # 160 bits, as RFC 4226 recommends

#: Crockford-style alphabet without look-alikes (no 0/o, 1/l/i, u).
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstvwxyz23456789"
RECOVERY_CODE_COUNT = 10
_RECOVERY_LENGTH = 10  # 10 symbols of ~4.9 bits each


def new_secret() -> str:
    """A fresh shared secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret: str, step: int) -> str:
    """The code for one time step (RFC 4226 dynamic truncation)."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


def normalise_code(code: str) -> str:
    return "".join(ch for ch in code if not ch.isspace() and ch != "-")


def verify(
    secret: str, code: str, *, last_step: int | None = None, now: float | None = None
) -> int | None:
    """The step ``code`` belongs to, or None. Steps at or before ``last_step`` are refused."""
    candidate = normalise_code(code)
    if len(candidate) != DIGITS or not candidate.isdigit():
        return None
    centre = current_step(now)
    matched = None
    # Compare every step in the window, so the time taken does not reveal which matched.
    for step in range(centre - WINDOW, centre + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), candidate) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def provisioning_uri(secret: str, *, account: str, issuer: str) -> str:
    """The ``otpauth://`` URI an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def qr_svg(data: str) -> str:
    """The QR code for ``data`` as an inline SVG document. Rendered here, so no browser
    library and no network is involved."""
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(data, error="m").save(
        buffer, kind="svg", scale=5, border=2, xmldecl=False, svgns=True, dark="#000", light="#fff"
    )
    return buffer.getvalue().decode("utf-8")


# -- recovery codes ----------------------------------------------------------------------
def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Fresh codes shown once, formatted ``abcde-fghjk`` for reading aloud."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalise_code(code).lower().encode("ascii", "ignore")).hexdigest()


def looks_like_recovery_code(code: str) -> bool:
    candidate = normalise_code(code).lower()
    return len(candidate) == _RECOVERY_LENGTH and all(c in _RECOVERY_ALPHABET for c in candidate)
