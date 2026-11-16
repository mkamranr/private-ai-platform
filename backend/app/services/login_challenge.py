"""A proof-of-work check on sign-in after repeated failures from an address (E29).

The platform runs air-gapped, so a hosted CAPTCHA is not an option: it would need the
internet, and send every sign-in to a third party. Instead, once an address has failed
``auth.login_challenge_after`` times in the throttle window, `/auth/login` asks for a
small proof of work: find a ``solution`` such that ``SHA-256(token + ":" + solution)``
starts with ``bits`` zero bits. A browser finds one in about a second; a guesser pays
that second for every guess, and each further five failures double it, up to 24 bits
(about 15 seconds in a browser, the most a person should be asked to wait).

The challenge is stateless: ``token`` is ``nonce.bits.expires`` signed with an HMAC of
the JWT secret, so issuing one stores nothing. A solved token is spent once, in Valkey;
with Valkey down the failure counts behind the check are not available either, so the
check is not asked for (the throttle fails open the same way).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

#: How long a challenge may take to solve and submit.
TTL_SECONDS = 300
#: Extra bits at most: each doubles the work, so the hardest is 16x the first.
MAX_EXTRA_BITS = 4
#: Never more than this, whatever the base: a person is waiting.
MAX_BITS = 24
#: Further failures per extra bit.
FAILURES_PER_BIT = 5
_SPENT_PREFIX = "auth:login-challenge:"


@dataclass(frozen=True, slots=True)
class Challenge:
    token: str
    bits: int
    expires_in: int

    def as_details(self) -> dict[str, Any]:
        return {
            "challenge": {
                "token": self.token,
                "bits": self.bits,
                "expires_in": self.expires_in,
                "algorithm": "sha256-leading-zero-bits",
            }
        }


def required_bits(failures: int, *, after: int, base_bits: int) -> int | None:
    """The difficulty an address with ``failures`` must solve, or None for none."""
    if after <= 0 or failures < after:
        return None
    extra = min(MAX_EXTRA_BITS, (failures - after) // FAILURES_PER_BIT)
    return min(MAX_BITS, base_bits + extra)


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + (8 - byte.bit_length())
    return bits


class LoginChallenges:
    def __init__(self, secret: str, redis: Any) -> None:
        self._key = hashlib.sha256(b"login-challenge:" + secret.encode()).digest()
        self._redis = redis

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, bits: int, *, now: float | None = None) -> Challenge:
        expires = int((now or time.time()) + TTL_SECONDS)
        payload = f"{secrets.token_hex(16)}.{bits}.{expires}"
        return Challenge(f"{payload}.{self._sign(payload)}", bits, TTL_SECONDS)

    async def accept(
        self, token: str, solution: str, *, min_bits: int, now: float | None = None
    ) -> bool:
        """True if ``solution`` solves ``token``, a challenge of at least ``min_bits``
        this platform issued, unexpired and not spent. Spends it."""
        try:
            nonce, bits_text, expires_text, signature = token.split(".")
            bits, expires = int(bits_text), int(expires_text)
        except ValueError:
            return False
        payload = f"{nonce}.{bits}.{expires}"
        if not hmac.compare_digest(signature, self._sign(payload)):
            return False
        if expires < (now or time.time()) or bits < min_bits:
            return False
        digest = hashlib.sha256(f"{token}:{solution}".encode()).digest()
        if leading_zero_bits(digest) < bits:
            return False
        return await self._spend(nonce, expires)

    async def _spend(self, nonce: str, expires: int) -> bool:
        """Once only. A replayed solution is refused; Valkey down lets it through."""
        if self._redis is None:
            return True
        try:
            ttl = max(1, expires - int(time.time()))
            return bool(await self._redis.set(f"{_SPENT_PREFIX}{nonce}", 1, nx=True, ex=ttl))
        except Exception as exc:
            log.warning("login_challenge_spend_unavailable", exc_type=type(exc).__name__)
            return True
