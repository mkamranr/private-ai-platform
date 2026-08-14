"""Concrete authentication providers (M03, Phase 6).

Three implementations of the §28 boundary in ``app.core.interfaces.auth_provider``:

============  ==========  ==================================================
provider      kind        the credential is seen by
============  ==========  ==================================================
``local``     password    the platform (argon2id hash in ``users``)
``ldap``      password    **only the directory** — the platform binds as the
                          person and never stores or hashes what it received
``oidc``      redirect    **only the IdP** — the platform receives a code
============  ==========  ==================================================

Every one of them produces an :class:`ExternalIdentity` and stops there. What that
identity is *allowed* to do is decided by :mod:`app.services.federation` from
operator-controlled configuration, because a directory group is a claim from outside the
platform and must not be a privilege grant on its own.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from app.config.settings import LdapSettings, OidcSettings, Settings
from app.core.errors import AuthenticationError, ValidationError
from app.core.interfaces.auth_provider import (
    AuthProvider,
    ExternalIdentity,
    PasswordAuthProvider,
    RedirectAuthProvider,
)
from app.core.logging import get_logger
from app.core.security import PasswordHasherService
from app.repositories.user import UserRepository

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------
class LocalAuthProvider(PasswordAuthProvider):
    """Username and password against the platform's own ``users`` table.

    Always present, and deliberately impossible to disable. An air-gapped site whose IdP
    is down has no vendor to call and no second channel; without a local account that can
    still sign in, the platform would be unadministrable exactly when someone needs to
    fix it.
    """

    name = "local"
    display_name = "Platform account"

    def __init__(self, users: UserRepository, hasher: PasswordHasherService) -> None:
        self._users = users
        self._hasher = hasher

    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        user = await self._users.get_by_username(username)
        if user is None or user.hashed_password is None or user.auth_provider != "local":
            return None
        if not self._hasher.verify(user.hashed_password, password):
            return None
        return ExternalIdentity(
            subject=str(user.id),
            username=user.username,
            email=user.email,
            provider=self.name,
            full_name=user.full_name,
        )

    async def health(self) -> tuple[bool, str]:
        return True, "Local accounts are always available."


# ---------------------------------------------------------------------------
# LDAP / Active Directory
# ---------------------------------------------------------------------------
class LdapAuthProvider(PasswordAuthProvider):
    """Authenticate by **binding as the person** against a directory.

    Not a lookup-and-compare: the directory is the only thing that ever sees the password,
    so the platform never holds a credential for a directory account and has nothing to
    leak if its own database is dumped.

    The ldap3 calls are synchronous, so each one runs in a worker thread. Inline, a
    directory that has gone unreachable would stall the whole event loop for the connect
    timeout — every request, not just sign-ins — which is how one dependency's outage
    becomes the platform's.
    """

    name = "ldap"

    def __init__(self, settings: LdapSettings) -> None:
        self._settings = settings
        self.display_name = settings.display_name

    async def authenticate(self, username: str, password: str) -> ExternalIdentity | None:
        # An empty password must never reach the directory: LDAP treats a simple bind with
        # an empty password as an *anonymous* bind, which succeeds. That would turn a blank
        # password box into a valid sign-in for any username in the directory.
        if not password:
            return None
        return await asyncio.to_thread(self._authenticate_sync, username, password)

    def _bind_mode(self) -> str:
        """AUTO_BIND_TLS_BEFORE_BIND when start_tls is on, plain bind otherwise.

        Named rather than inlined because getting it wrong sends the password over the
        wire in clear text, and that is not a thing to encode in operator precedence.
        """
        import ldap3

        mode = (
            ldap3.AUTO_BIND_TLS_BEFORE_BIND if self._settings.start_tls else ldap3.AUTO_BIND_NO_TLS
        )
        return str(mode)

    def _authenticate_sync(self, username: str, password: str) -> ExternalIdentity | None:
        import ldap3
        from ldap3.core.exceptions import LDAPException

        cfg = self._settings
        try:
            server = ldap3.Server(
                cfg.server_uri, get_info=ldap3.NONE, connect_timeout=cfg.timeout_seconds
            )
            # Search first, as the service account: the person's DN is not derivable from
            # their username in any directory the platform does not control.
            with ldap3.Connection(
                server,
                user=cfg.bind_dn or None,
                password=(cfg.bind_password.get_secret_value() if cfg.bind_password else None),
                auto_bind=self._bind_mode(),
                receive_timeout=cfg.timeout_seconds,
            ) as search_conn:
                search_conn.search(
                    cfg.user_search_base,
                    cfg.user_filter.format(username=_escape_filter(username)),
                    attributes=[cfg.email_attribute, cfg.full_name_attribute],
                )
                if not search_conn.entries:
                    return None  # no such person here — the caller may try another provider
                entry = search_conn.entries[0]
                user_dn = entry.entry_dn
                email = _attr(entry, cfg.email_attribute) or f"{username}@invalid.local"
                full_name = _attr(entry, cfg.full_name_attribute)

            # Now bind as the person. This is the actual credential check.
            with ldap3.Connection(
                server,
                user=user_dn,
                password=password,
                auto_bind=self._bind_mode(),
                receive_timeout=cfg.timeout_seconds,
            ):
                pass
        except LDAPException as exc:
            # Bind failure and directory outage are both LDAPException. They are separated
            # by whether the search above found the person: it did, so the password is
            # wrong. Reported as "no match" so the caller falls through to a generic
            # failure rather than telling the world which usernames exist.
            log.info("ldap_bind_failed", username=username, error=str(exc))
            return None

        groups = self._read_groups(user_dn)
        return ExternalIdentity(
            subject=user_dn,
            username=username,
            email=email,
            provider=self.name,
            full_name=full_name,
            groups=groups,
        )

    def _read_groups(self, user_dn: str) -> tuple[str, ...]:
        import ldap3
        from ldap3.core.exceptions import LDAPException

        cfg = self._settings
        if not cfg.group_search_base:
            return ()
        try:
            server = ldap3.Server(
                cfg.server_uri, get_info=ldap3.NONE, connect_timeout=cfg.timeout_seconds
            )
            with ldap3.Connection(
                server,
                user=cfg.bind_dn or None,
                password=(cfg.bind_password.get_secret_value() if cfg.bind_password else None),
                auto_bind=True,
                receive_timeout=cfg.timeout_seconds,
            ) as conn:
                conn.search(
                    cfg.group_search_base,
                    cfg.group_filter.format(user_dn=_escape_filter(user_dn)),
                    attributes=["cn"],
                )
                return tuple(str(e.cn) for e in conn.entries if hasattr(e, "cn"))
        except LDAPException as exc:
            # Groups drive role assignment, so failing to read them must not look like
            # "this person is in no groups" — that would silently strip their access on
            # the next sign-in. Raised, so the login fails and says why.
            raise AuthenticationError(
                "Signed in, but the directory could not be asked which groups you are in, "
                "so your access could not be determined. Try again shortly."
            ) from exc

    async def health(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self._health_sync)

    def _health_sync(self) -> tuple[bool, str]:
        import ldap3
        from ldap3.core.exceptions import LDAPException

        cfg = self._settings
        try:
            server = ldap3.Server(
                cfg.server_uri, get_info=ldap3.NONE, connect_timeout=cfg.timeout_seconds
            )
            with ldap3.Connection(
                server,
                user=cfg.bind_dn or None,
                password=(cfg.bind_password.get_secret_value() if cfg.bind_password else None),
                auto_bind=True,
                receive_timeout=cfg.timeout_seconds,
            ):
                return True, f"Bound to {cfg.server_uri} as the service account."
        except LDAPException as exc:
            return False, f"Could not bind to {cfg.server_uri}: {exc}"


def _attr(entry: Any, name: str) -> str | None:
    value = getattr(entry, name, None)
    text = str(value) if value is not None else ""
    return text or None


def _escape_filter(value: str) -> str:
    """Escape RFC 4515 filter metacharacters.

    Without this a username of ``*`` matches every entry in the directory, and
    ``x)(|(uid=*`` rewrites the filter entirely — LDAP injection, the same class of bug as
    SQL injection and just as available from a login form.
    """
    for char, replacement in (
        ("\\", r"\5c"),
        ("*", r"\2a"),
        ("(", r"\28"),
        (")", r"\29"),
        ("\0", r"\00"),
    ):
        value = value.replace(char, replacement)
    return value


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------
class OidcAuthProvider(RedirectAuthProvider):
    """OpenID Connect authorization-code flow against an internal IdP (Keycloak).

    Endpoints and signing keys come from the provider's discovery document rather than
    from configuration, so a key rotation at the IdP does not require a platform config
    change — and cannot be missed until every sign-in starts failing.
    """

    name = "oidc"

    def __init__(self, settings: OidcSettings, client: httpx.AsyncClient) -> None:
        self.display_name = settings.display_name
        self._settings = settings
        self._client = client
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def _discover(self) -> dict[str, Any]:
        if self._discovery is not None:
            return self._discovery
        async with self._lock:
            if self._discovery is not None:
                return self._discovery
            url = self._settings.issuer.rstrip("/") + "/.well-known/openid-configuration"
            response = await self._client.get(url, timeout=10.0)
            response.raise_for_status()
            self._discovery = response.json()
            return self._discovery

    async def _signing_key(self, token: str) -> Any:
        """Find the key the token was signed with, refetching once if it is unknown.

        The refetch is what makes an IdP key rotation invisible here: a token signed with a
        key minted after the last fetch would otherwise be rejected as forged until the
        platform restarted.
        """
        kid = jwt.get_unverified_header(token).get("kid")
        for attempt in (0, 1):
            jwks = await self._get_jwks(force=attempt == 1)
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    return RSAAlgorithm.from_jwk(key)
        raise AuthenticationError(
            "The identity provider signed this token with a key it does not publish."
        )

    async def _get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if self._jwks is not None and not force and time.monotonic() - self._jwks_fetched_at < 3600:
            return self._jwks
        discovery = await self._discover()
        response = await self._client.get(discovery["jwks_uri"], timeout=10.0)
        response.raise_for_status()
        self._jwks = response.json()
        self._jwks_fetched_at = time.monotonic()
        return self._jwks

    async def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        discovery = await self._discover()
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self._settings.scopes),
                "state": state,
            }
        )
        return f"{discovery['authorization_endpoint']}?{params}"

    async def exchange(self, *, code: str, redirect_uri: str) -> ExternalIdentity:
        discovery = await self._discover()
        secret = self._settings.client_secret
        response = await self._client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self._settings.client_id,
                **({"client_secret": secret.get_secret_value()} if secret else {}),
            },
            timeout=15.0,
        )
        if response.status_code >= 400:
            raise AuthenticationError(
                f"The identity provider rejected the sign-in: {response.text[:200]}"
            )

        id_token = response.json().get("id_token")
        if not id_token:
            raise AuthenticationError(
                "The identity provider returned no id_token. Check that the 'openid' "
                "scope is granted to this client."
            )

        # Verified, not decoded. jwt.decode with verify_signature disabled would accept a
        # token anyone could mint, which is the whole attack this flow exists to prevent.
        claims = jwt.decode(
            id_token,
            key=await self._signing_key(id_token),
            algorithms=["RS256", "RS384", "RS512"],
            audience=self._settings.client_id,
            issuer=discovery.get("issuer", self._settings.issuer),
            leeway=self._settings.leeway_seconds,
        )

        username = claims.get(self._settings.username_claim) or claims.get("sub")
        email = claims.get("email")
        if not email:
            raise AuthenticationError(
                "The identity provider returned no email claim, which the platform uses to "
                "identify accounts. Grant the 'email' scope to this client."
            )

        raw_groups = claims.get(self._settings.groups_claim) or []
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]

        return ExternalIdentity(
            subject=str(claims["sub"]),
            username=str(username),
            email=str(email),
            provider=self.name,
            full_name=claims.get("name"),
            # Keycloak emits group paths ("/ai-platform/admins"); the leaf is what an
            # operator sees in the console and therefore what they will map.
            groups=tuple(str(g).rsplit("/", 1)[-1] for g in raw_groups),
        )

    async def health(self) -> tuple[bool, str]:
        try:
            discovery = await self._discover()
        except Exception as exc:
            return False, f"Discovery failed for {self._settings.issuer}: {exc}"
        return True, f"Discovered {discovery.get('issuer', self._settings.issuer)}."


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def build_providers(
    settings: Settings,
    users: UserRepository,
    hasher: PasswordHasherService,
    http: httpx.AsyncClient,
) -> list[AuthProvider]:
    """Local first, then whatever is configured and enabled.

    Order is the order tried. Local is first and unconditional — see LocalAuthProvider.
    """
    providers: list[AuthProvider] = [LocalAuthProvider(users, hasher)]

    if settings.ldap.enabled:
        if not settings.ldap.server_uri or not settings.ldap.user_search_base:
            raise ValidationError(
                "LDAP__ENABLED is true but LDAP__SERVER_URI or LDAP__USER_SEARCH_BASE is "
                "empty. A half-configured provider fails at the first sign-in attempt "
                "rather than at startup, so it is refused here."
            )
        providers.append(LdapAuthProvider(settings.ldap))

    if settings.oidc.enabled:
        if not settings.oidc.issuer or not settings.oidc.client_id:
            raise ValidationError(
                "OIDC__ENABLED is true but OIDC__ISSUER or OIDC__CLIENT_ID is empty."
            )
        providers.append(OidcAuthProvider(settings.oidc, http))

    return providers
