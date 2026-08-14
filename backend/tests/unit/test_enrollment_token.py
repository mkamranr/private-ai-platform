"""Enrolment token minting (M04).

The one property worth a test file of its own is the storage direction: an enrolment token
is **hashed**, because the platform verifies what a node presents. The agent token beside
it is **encrypted**, because the platform presents that one to the agent. Getting the two
the wrong way round is the easy mistake, and it fails silently — an encrypted enrolment
token still "works", it just means a database dump yields live credentials.
"""

from __future__ import annotations

from app.core.security import (
    generate_api_key,
    generate_enrollment_token,
    hash_api_key,
    verify_api_key,
)


def test_token_is_namespaced_so_a_leak_is_identifiable() -> None:
    full, _, _ = generate_enrollment_token()
    assert full.startswith("aine_")


def test_not_confusable_with_an_api_key() -> None:
    """Distinct prefixes: finding one in a log should say which kind it is."""
    assert not generate_enrollment_token()[0].startswith("aip_")
    assert not generate_api_key()[0].startswith("aine_")


def test_every_mint_is_unique() -> None:
    assert len({generate_enrollment_token()[0] for _ in range(50)}) == 50


def test_the_stored_hash_is_not_the_token() -> None:
    full, prefix, stored = generate_enrollment_token()
    assert stored != full
    assert full not in stored
    # The prefix is for display and must not be enough to reconstruct anything.
    assert prefix in full
    assert len(stored) == 64


def test_verification_round_trips_and_rejects_anything_else() -> None:
    full, _, stored = generate_enrollment_token()
    assert verify_api_key(full, stored)
    assert not verify_api_key(full + "x", stored)
    assert not verify_api_key(generate_enrollment_token()[0], stored)


def test_hashing_is_the_same_function_used_for_lookup() -> None:
    """The consume path looks a row up by `hash_api_key(presented)`; it must agree."""
    full, _, stored = generate_enrollment_token()
    assert hash_api_key(full) == stored
