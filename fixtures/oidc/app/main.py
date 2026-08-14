"""A fixture OpenID Connect provider, for developing and testing SSO without one (§M26).

The same reasoning as ``mock-vllm`` and the fixture LDAP server: the reference development
machine has no Keycloak, and an OIDC integration that is only ever exercised against a real
IdP is an integration nobody can test until the day it has to work.

It implements just enough of OIDC for the platform's flow to be real end to end:

* ``/.well-known/openid-configuration`` — discovery
* ``/authorize``  — **auto-approves**, immediately redirecting back with a code
* ``/token``      — exchanges the code for a properly **RS256-signed** id_token
* ``/jwks``       — the public key, so the platform verifies a real signature

What matters is that the signature is genuine. A fixture that returned an unsigned token
would let the platform's verification be broken without any test noticing — and that
verification is the entire security of the flow.

**Never deploy this.** It authenticates nobody: any username in ``USERS`` is issued a token
on request, with no password at all. It runs under the ``development`` compose profile only.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import RedirectResponse

ISSUER = "http://oidc-fixture:8000"
CLIENT_ID = "ai-platform"

#: The fixture directory. Groups are what the platform maps to roles, so these names are
#: what an operator would put in FEDERATION__ROLE_MAPPING when trying the flow out.
USERS: dict[str, dict[str, Any]] = {
    "fatima.almansoori": {
        "sub": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "email": "fatima.almansoori@example.ae",
        "name": "Fatima Al Mansoori",
        "groups": ["ai-platform-users", "ai-platform-agent-admins"],
    },
    "omar.alblooshi": {
        "sub": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "email": "omar.alblooshi@example.ae",
        "name": "Omar Al Blooshi",
        "groups": ["ai-platform-users"],
    },
    # Deliberately in no group the platform maps: exercises the "signs in, gets the
    # default role" path, which is easy to break and invisible if every fixture user is
    # privileged.
    "guest.user": {
        "sub": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        "email": "guest.user@example.ae",
        "name": "Guest User",
        "groups": ["everyone"],
    },
}

app = FastAPI(
    title="Fixture OIDC provider",
    version="0.1.0",
    description=__doc__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Generated per process. A key baked into the image would be a published private key in
# every copy of the repository, and someone would eventually point a real deployment here.
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = uuid.uuid4().hex
_CODES: dict[str, str] = {}


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "issuer": ISSUER, "warning": "fixture IdP — authenticates nobody"}


@app.get("/.well-known/openid-configuration")
async def discovery() -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email", "groups"],
        "claims_supported": ["sub", "email", "name", "preferred_username", "groups"],
    }


@app.get("/jwks")
async def jwks() -> dict[str, Any]:
    numbers = _KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": _KID,
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }


@app.get("/authorize")
async def authorize(
    redirect_uri: str = Query(...),
    state: str = Query(""),
    client_id: str = Query(CLIENT_ID),
    login: str = Query("fatima.almansoori", description="Which fixture user to sign in as."),
    response_type: str = Query("code"),
    scope: str = Query("openid"),
) -> RedirectResponse:
    """Auto-approve and bounce straight back.

    A real IdP shows a login form here. Skipping it is what makes the flow scriptable in a
    gate — the platform's half of the exchange is identical either way, and the platform's
    half is what is under test.
    """
    if login not in USERS:
        raise HTTPException(400, f"No fixture user {login!r}. Known: {', '.join(USERS)}")
    code = uuid.uuid4().hex
    _CODES[code] = login
    return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)


@app.post("/token")
async def token(
    code: str = Form(...),
    grant_type: str = Form("authorization_code"),
    redirect_uri: str = Form(""),
    client_id: str = Form(CLIENT_ID),
    client_secret: str = Form(""),
) -> dict[str, Any]:
    # Single use, like a real one: a replayed code must not mint a second token.
    login = _CODES.pop(code, None)
    if login is None:
        raise HTTPException(400, "invalid_grant: unknown or already-used code")

    user = USERS[login]
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": client_id,
        "sub": user["sub"],
        "exp": now + 300,
        "iat": now,
        "preferred_username": login,
        "email": user["email"],
        "email_verified": True,
        "name": user["name"],
        "groups": user["groups"],
    }
    id_token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": _KID})
    return {
        "access_token": uuid.uuid4().hex,
        "token_type": "Bearer",
        "expires_in": 300,
        "id_token": id_token,
    }


@app.get("/fixture/users")
async def fixture_users() -> dict[str, Any]:
    """Who this fixture knows about, so a gate does not have to hard-code them."""
    return {
        "warning": "Fixture provider. Any of these is issued a token with no password.",
        "users": {
            name: {"email": u["email"], "groups": u["groups"]} for name, u in USERS.items()
        },
    }


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps({"issuer": ISSUER, "users": list(USERS)}, indent=2))
