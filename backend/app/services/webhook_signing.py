"""Signed workflow webhooks (expansion E36).

The webhook token (E6) says *who may call*. It does not say the body arrived as it was
sent, and a captured call can be sent again. A signature covers both:

    X-Workflow-Timestamp: 1763366400            (Unix seconds, when it was signed)
    X-Workflow-Signature: sha256=<hex>          (HMAC-SHA256 of "<timestamp>.<raw body>")

keyed with the workflow's signing secret, which is issued with its token. The timestamp is
inside the signature, so it cannot be changed; a call older than :data:`TOLERANCE_SECONDS`
(or that far in the future) is refused, and within that window the route refuses a
signature it has seen before. This is the scheme Stripe and GitHub use, so most senders
can produce it with a few lines.

Pure functions: no I/O, so the rules are tested directly.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: How far the signed timestamp may be from the platform's clock, either way.
TOLERANCE_SECONDS = 300
PREFIX = "sha256="


class SignatureError(Exception):
    """Why a signature was refused. The message is for the log, not the caller."""


def new_secret() -> str:
    return "wfs_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int | str, body: bytes) -> str:
    """The ``X-Workflow-Signature`` value for this body at this time."""
    message = str(timestamp).encode() + b"." + body
    return PREFIX + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    *,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    now: float,
) -> None:
    """Raise :class:`SignatureError` unless ``signature`` is valid and fresh."""
    if not timestamp or not signature:
        raise SignatureError("missing X-Workflow-Timestamp or X-Workflow-Signature")
    try:
        when = int(timestamp)
    except ValueError as exc:
        raise SignatureError("timestamp is not an integer") from exc
    if abs(now - when) > TOLERANCE_SECONDS:
        raise SignatureError("timestamp outside the tolerance")
    expected = sign(secret, when, body)
    # Several signatures may be sent while a secret is being rotated on the sender side;
    # one of them matching is enough.
    offered = [part.strip() for part in signature.split(",")]
    if not any(hmac.compare_digest(expected, part) for part in offered):
        raise SignatureError("signature does not match")
