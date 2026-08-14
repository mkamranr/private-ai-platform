"""AuthProvider — where the platform believes an identity comes from (M03, §28).

The platform's own JWT stays the internal representation of a session no matter who
vouched for the person. A provider's only job is to turn a credential into an
:class:`ExternalIdentity`; everything downstream — permissions, audit, the gateway — reads
the platform's ``User`` row and never learns which provider was involved.

That boundary is why adding Keycloak or Active Directory in Phase 6 changed no route, no
permission check and no audit record.

Two kinds, because they are genuinely different interactions and pretending otherwise
produces an interface that lies:

``PasswordAuthProvider``
    The platform receives the credential and checks it — local passwords, an LDAP bind.
    One request, no browser involved, usable from the CLI.

``RedirectAuthProvider``
    The platform never sees the credential. The person authenticates at the identity
    provider and comes back with a code — OIDC. Requires a browser and a round trip.

An air-gapped site has no internet, but it does have an internal IdP, so both are real
deployments rather than one being an online-only luxury.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AuthProviderKind(enum.StrEnum):
    PASSWORD = "PASSWORD"  # noqa: S105 — a flow name, not a credential
    REDIRECT = "REDIRECT"


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """What a provider asserts about a person.

    Deliberately *not* a ``User``. This is a claim from outside the platform, and the
    difference matters: ``groups`` is whatever the directory said, and the platform decides
    separately — from operator-controlled configuration — what that entitles anyone to.
    Treating it as a user row would make an AD group name a permission grant.
    """

    #: Stable identifier at the provider. Not the username: usernames get reassigned when
    #: people leave, and reusing one would silently hand a new joiner the old holder's
    #: history and roles.
    subject: str
    username: str
    email: str
    provider: str
    full_name: str | None = None
    groups: tuple[str, ...] = field(default_factory=tuple)


class AuthProvider(ABC):
    """Common surface. Implementations are constructed from settings, never from a request."""

    #: Stable key, stored on the user row and used in configuration.
    name: str
    #: Shown on the sign-in page.
    display_name: str
    kind: AuthProviderKind

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a subclass that forgets an attribute.

        A bare annotation is documentation, not a promise — `display_name: str` on this
        class creates nothing, so a provider that never assigns it raises AttributeError
        the first time the sign-in page is rendered, in a route, far from the cause.
        Checked here so it is a startup error instead.

        Skipped for the two intermediate classes, which legitimately supply only `kind`.
        """
        super().__init_subclass__(**kwargs)
        if cls.__name__ in ("PasswordAuthProvider", "RedirectAuthProvider"):
            return
        if getattr(cls, "__abstractmethods__", None):
            return  # still abstract; a further subclass will be checked instead
        for attribute in ("name", "kind"):
            if not isinstance(getattr(cls, attribute, None), str):
                raise TypeError(
                    f"{cls.__name__} must set a class-level {attribute!r}. "
                    "AuthProvider only annotates it; the annotation creates nothing."
                )

    @abstractmethod
    async def health(self) -> tuple[bool, str]:
        """``(reachable, explanation)``.

        Part of the interface because an unreachable IdP is indistinguishable from a wrong
        password to everyone except the person who can fix it, and an air-gapped site has
        no vendor status page to check.
        """
        ...


class PasswordAuthProvider(AuthProvider):
    """The platform receives the credential and verifies it."""

    kind = AuthProviderKind.PASSWORD

    @abstractmethod
    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        """Verify a credential.

        Returns ``None`` when this provider has no such user — distinct from raising, which
        means the provider itself failed. The caller can try the next provider on ``None``
        but must not on an error, because "the directory is down" must never silently
        become "wrong password".
        """
        ...


class RedirectAuthProvider(AuthProvider):
    """The person authenticates at the provider; the platform sees only a code."""

    kind = AuthProviderKind.REDIRECT

    @abstractmethod
    async def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        """Where to send the browser to start sign-in."""
        ...

    @abstractmethod
    async def exchange(self, *, code: str, redirect_uri: str) -> ExternalIdentity:
        """Turn the returned code into an identity.

        Implementations must verify the token's signature against the provider's published
        keys, plus issuer and audience. A decoded-but-unverified token is an identity
        anyone can mint.
        """
        ...
