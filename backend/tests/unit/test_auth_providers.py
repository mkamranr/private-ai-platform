"""Provider-level guards that a working directory would never reveal (M03, Phase 6).

Both tests here cover behaviour that looks identical to "it works" from the outside. A
site with a healthy AD passes its sign-in test whether or not either guard exists; the
first time they matter is the first time someone tries.
"""

from __future__ import annotations

import pytest

from app.config.settings import LdapSettings
from app.services.auth_providers import LdapAuthProvider, _escape_filter


class _ExplodingProvider(LdapAuthProvider):
    """Fails loudly if anything reaches the directory.

    The point of these tests is that certain inputs must be rejected *before* a connection
    is attempted, so the assertion is "no directory call happened at all".
    """

    def _authenticate_sync(self, username: str, password: str):  # type: ignore[no-untyped-def]
        raise AssertionError(
            f"The directory was contacted for username={username!r} — it should have been "
            "refused before any connection was made."
        )


@pytest.mark.asyncio
async def test_an_empty_password_never_reaches_the_directory() -> None:
    """LDAP treats a simple bind with an empty password as an **anonymous** bind, which
    succeeds. Without this guard, submitting a blank password box signs you in as any
    username that exists in the directory — while the platform's own logs record a
    perfectly ordinary successful login."""
    provider = _ExplodingProvider(LdapSettings(enabled=True, server_uri="ldap://example.invalid"))

    assert await provider.authenticate("fatima.almansoori", "") is None


def test_filter_metacharacters_are_escaped() -> None:
    """LDAP injection, reachable from a login form. `*` alone matches every entry in the
    directory; the second case rewrites the filter to match any user at all."""
    assert _escape_filter("*") == r"\2a"
    assert _escape_filter("x)(|(uid=*") == r"x\29\28|\28uid=\2a"
    # Backslash first, or the escapes would themselves be escaped.
    assert _escape_filter(r"a\b") == r"a\5cb"
    # Ordinary names pass through untouched — an escape that mangled real usernames would
    # be discovered as "AD login is broken", not as a security control working.
    assert _escape_filter("fatima.almansoori") == "fatima.almansoori"


def test_start_tls_off_does_not_silently_become_tls_on() -> None:
    """The bind mode was originally written as `a and b or c`, which happens to work but
    reads as a puzzle — and the failure mode is sending the password in clear text."""
    import ldap3

    with_tls = LdapAuthProvider(LdapSettings(start_tls=True))
    without = LdapAuthProvider(LdapSettings(start_tls=False))

    assert with_tls._bind_mode() == ldap3.AUTO_BIND_TLS_BEFORE_BIND
    assert without._bind_mode() == ldap3.AUTO_BIND_NO_TLS
