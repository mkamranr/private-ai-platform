"""The webhook signature rules (E36), without a database."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.services import webhook_signing as ws

SECRET = "wfs_test"
NOW = 1_763_366_400
BODY = b'{"order": "SO-88140"}'


def test_the_signature_is_hmac_sha256_of_timestamp_dot_body() -> None:
    expected = hmac.new(SECRET.encode(), b"1763366400." + BODY, hashlib.sha256).hexdigest()
    assert ws.sign(SECRET, NOW, BODY) == "sha256=" + expected


def test_a_fresh_valid_signature_passes() -> None:
    ws.verify(
        SECRET, body=BODY, timestamp=str(NOW), signature=ws.sign(SECRET, NOW, BODY), now=NOW + 10
    )


@pytest.mark.parametrize(
    ("body", "timestamp", "signature", "why"),
    [
        (BODY, None, "sha256=x", "missing"),
        (BODY, str(NOW), None, "missing"),
        (BODY, "soon", "sha256=x", "integer"),
        (b'{"order": "SO-99999"}', str(NOW), ws.sign(SECRET, NOW, BODY), "does not match"),
        (BODY, str(NOW + 1), ws.sign(SECRET, NOW, BODY), "does not match"),
        (BODY, str(NOW), ws.sign("wfs_other", NOW, BODY), "does not match"),
    ],
)
def test_tampered_missing_or_wrong_is_refused(body, timestamp, signature, why) -> None:
    with pytest.raises(ws.SignatureError, match=why):
        ws.verify(SECRET, body=body, timestamp=timestamp, signature=signature, now=NOW)


@pytest.mark.parametrize("offset", [ws.TOLERANCE_SECONDS + 1, -(ws.TOLERANCE_SECONDS + 1)])
def test_too_old_or_too_far_ahead_is_refused(offset) -> None:
    signature = ws.sign(SECRET, NOW, BODY)
    with pytest.raises(ws.SignatureError, match="tolerance"):
        ws.verify(SECRET, body=BODY, timestamp=str(NOW), signature=signature, now=NOW + offset)


def test_one_of_several_signatures_is_enough() -> None:
    both = f"sha256=deadbeef, {ws.sign(SECRET, NOW, BODY)}"
    ws.verify(SECRET, body=BODY, timestamp=str(NOW), signature=both, now=NOW)


def test_secrets_are_random_and_marked() -> None:
    first, second = ws.new_secret(), ws.new_secret()
    assert first != second and first.startswith("wfs_") and len(first) > 40
