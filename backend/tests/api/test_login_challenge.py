"""A proof-of-work check on sign-in after repeated failures from an address (E29)."""

from __future__ import annotations

import hashlib
import time
from itertools import count

import pytest
from httpx import AsyncClient

from app.services.login_challenge import (
    MAX_BITS,
    LoginChallenges,
    leading_zero_bits,
    required_bits,
)
from tests.api.test_auth_hardening import _login
from tests.conftest import TEST_PASSWORD

WRONG = "definitely-the-wrong-one"


def _solve(token: str, bits: int) -> str:
    for n in count():
        if leading_zero_bits(hashlib.sha256(f"{token}:{n}".encode()).digest()) >= bits:
            return str(n)
    raise AssertionError("unreachable")


class FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, value: object, *, nx: bool, ex: int) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class TestDifficulty:
    def test_none_below_the_threshold_or_when_off(self) -> None:
        assert required_bits(4, after=5, base_bits=20) is None
        assert required_bits(100, after=0, base_bits=20) is None

    def test_one_more_bit_per_five_failures_capped(self) -> None:
        assert required_bits(5, after=5, base_bits=16) == 16
        assert required_bits(9, after=5, base_bits=16) == 16
        assert required_bits(10, after=5, base_bits=16) == 17
        assert required_bits(1000, after=5, base_bits=16) == 20
        assert required_bits(1000, after=5, base_bits=22) == MAX_BITS

    def test_leading_zero_bits(self) -> None:
        assert leading_zero_bits(b"\x00\x00\xff") == 16
        assert leading_zero_bits(b"\x00\x10") == 11
        assert leading_zero_bits(b"\x80") == 0


class TestChallenges:
    async def test_a_solved_challenge_is_accepted_once(self) -> None:
        challenges = LoginChallenges("secret", FakeRedis())
        issued = challenges.issue(8)
        solution = _solve(issued.token, 8)
        assert await challenges.accept(issued.token, solution, min_bits=8)
        assert not await challenges.accept(issued.token, solution, min_bits=8), "replayed"

    async def test_refused_when_wrong_tampered_expired_or_too_easy(self) -> None:
        challenges = LoginChallenges("secret", FakeRedis())
        issued = challenges.issue(8)
        solution = _solve(issued.token, 8)
        wrong = next(
            str(n)
            for n in count()
            if leading_zero_bits(hashlib.sha256(f"{issued.token}:{n}".encode()).digest()) < 8
        )
        assert not await challenges.accept(issued.token, wrong, min_bits=8)
        # Another platform's secret, or the difficulty edited down.
        other = LoginChallenges("another-secret", FakeRedis())
        assert not await other.accept(issued.token, solution, min_bits=8)
        nonce, _bits, expires, signature = issued.token.split(".")
        easier = f"{nonce}.1.{expires}.{signature}"
        assert not await challenges.accept(easier, _solve(easier, 1), min_bits=1)
        # Issued for fewer bits than the address now needs.
        fresh = challenges.issue(8)
        assert not await challenges.accept(fresh.token, _solve(fresh.token, 8), min_bits=9)
        # Expired.
        old = challenges.issue(8, now=time.time() - 3600)
        assert not await challenges.accept(old.token, _solve(old.token, 8), min_bits=8)
        assert not await challenges.accept("garbage", "1", min_bits=8)


@pytest.fixture
def low_challenge(settings, monkeypatch):
    monkeypatch.setattr(settings.auth, "login_challenge_after", 2)
    monkeypatch.setattr(settings.auth, "login_challenge_bits", 8)
    monkeypatch.setattr(settings.auth, "lockout_threshold", 0)
    return settings


async def _fail_twice(client: AsyncClient, username: str) -> None:
    for _ in range(2):
        r = await _login(client, username, WRONG)
        assert r.status_code == 401
        assert r.json()["error"]["code"] != "challenge_required"


class TestSignIn:
    async def test_after_repeated_failures_a_check_is_asked_for_then_accepted(
        self, client: AsyncClient, committed_user, low_challenge
    ) -> None:
        user, _ = committed_user
        await _fail_twice(client, user.username)

        asked = await _login(client, user.username)
        assert asked.status_code == 401
        error = asked.json()["error"]
        assert error["code"] == "challenge_required"
        challenge = error["details"]["challenge"]
        assert challenge["bits"] == 8 and challenge["algorithm"] == "sha256-leading-zero-bits"

        solved = await client.post(
            "/api/v1/auth/login",
            json={
                "username": user.username,
                "password": TEST_PASSWORD,
                "challenge": {
                    "token": challenge["token"],
                    "solution": _solve(challenge["token"], challenge["bits"]),
                },
            },
        )
        assert solved.status_code == 200, solved.text

    async def test_the_check_does_not_depend_on_the_account(
        self, client: AsyncClient, committed_user, low_challenge
    ) -> None:
        """Per address: an unknown username gets the same answer, so it reveals nothing."""
        user, _ = committed_user
        await _fail_twice(client, user.username)
        unknown = await _login(client, "nobody-at-all", WRONG)
        assert unknown.json()["error"]["code"] == "challenge_required"

    async def test_a_wrong_solution_is_asked_again(
        self, client: AsyncClient, committed_user, low_challenge
    ) -> None:
        user, _ = committed_user
        await _fail_twice(client, user.username)
        r = await client.post(
            "/api/v1/auth/login",
            json={
                "username": user.username,
                "password": TEST_PASSWORD,
                "challenge": {"token": "a.8.1.b", "solution": "1"},
            },
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "challenge_required"

    async def test_off_when_zero(
        self, client: AsyncClient, committed_user, low_challenge, monkeypatch
    ) -> None:
        monkeypatch.setattr(low_challenge.auth, "login_challenge_after", 0)
        user, _ = committed_user
        await _fail_twice(client, user.username)
        assert (await _login(client, user.username)).status_code == 200
