"""Every route is authenticated and authorised — checked mechanically (E3).

Walks the OpenAPI schema rather than a hand-kept list, so a route added tomorrow is
covered tomorrow. Two passes:

* **No credentials** — every route must answer 401, except the short, explicit list of
  public routes below.
* **Authenticated, no permissions** — every management route must answer 403, except
  the routes any signed-in user may call.

Path parameters are filled with random UUIDs: authorisation is decided before any
lookup, so the resource not existing must not change the answer. A route that answers
404 to a permissionless caller here would be leaking whether ids exist.
"""

from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient

from app.models.auth import User
from tests.conftest import auth_header

#: Routes that answer without any credential, and why.
PUBLIC: set[tuple[str, str]] = {
    # Liveness and readiness probes: orchestrators call them with no identity.
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("GET", "/api/v1/version"),
    # Signing in, and the providers a sign-in page offers. OIDC answers 404 while it
    # is not configured.
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("GET", "/api/v1/auth/providers"),
    ("GET", "/api/v1/auth/oidc/authorize"),
    ("GET", "/api/v1/auth/oidc/callback"),
    # E6: the workflow webhook. Its token is the credential; a bad one is 401 all the same.
    ("POST", "/api/v1/workflow-hooks/{workflow_id}"),
}

#: A node enrolling presents a one-time enrolment token, not a user token. These answer
#: 401 to a missing token (checked by the first pass) and also to a user's JWT, which
#: they must reject (see test_node_enrollment).
ENROLMENT: set[tuple[str, str]] = {
    ("POST", "/api/v1/nodes/enroll"),
    ("GET", "/api/v1/nodes/enrollment-bundle"),
}

#: Routes any authenticated user may call, whatever their permissions.
ANY_AUTHENTICATED: set[tuple[str, str]] = {
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/whoami"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/users/me/password"),
    ("GET", "/api/v1/teams/mine"),
    # Checks the permission for the *kind* of resource being moved, which is only known
    # from the body — covered in test_teams.TestReassignment.
    ("PUT", "/api/v1/scope"),
}

_METHODS = {"get", "post", "put", "patch", "delete"}


def _routes(app) -> list[tuple[str, str]]:
    out = []
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            if method in _METHODS:
                out.append((method.upper(), path))
    return sorted(out)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), path)


async def _call(client: AsyncClient, method: str, path: str, headers: dict[str, str]) -> int:
    kwargs: dict = {"headers": headers}
    if method in {"POST", "PUT", "PATCH"}:
        kwargs["json"] = {}
    return (await client.request(method, _concrete(path), **kwargs)).status_code


class TestEveryRoute:
    async def test_without_credentials_every_non_public_route_is_401(
        self, app, client: AsyncClient
    ) -> None:
        wrong = []
        for method, path in _routes(app):
            if (method, path) in PUBLIC:
                continue
            status = await _call(client, method, path, {})
            # Enrolment is rate-limited per address before the token is read, so after
            # the enrolment tests it may answer 429 — a refusal all the same.
            allowed = {401, 429} if (method, path) in ENROLMENT else {401}
            if status not in allowed:
                wrong.append((method, path, status))
        assert not wrong, "routes reachable without credentials:\n" + "\n".join(map(str, wrong))

    async def test_without_permissions_every_management_route_is_403(
        self, app, client: AsyncClient, tokens, unprivileged_user: User
    ) -> None:
        headers = auth_header(tokens, unprivileged_user)
        wrong = []
        for method, path in _routes(app):
            if not path.startswith("/api/v1/"):
                continue  # the /v1 gateway takes API keys, not user tokens
            if (method, path) in PUBLIC | ANY_AUTHENTICATED | ENROLMENT:
                continue
            status = await _call(client, method, path, headers)
            if status != 403:
                wrong.append((method, path, status))
        assert not wrong, "routes not refusing a permissionless user:\n" + "\n".join(
            map(str, wrong)
        )


@pytest.mark.parametrize("route", sorted(PUBLIC | ANY_AUTHENTICATED | ENROLMENT))
def test_allowlists_name_real_routes(app, route: tuple[str, str]) -> None:
    """An allow-list entry for a route that no longer exists is a hole waiting for a
    future route of the same name."""
    assert route in set(_routes(app)), route
