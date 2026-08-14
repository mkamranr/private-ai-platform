# Chat (M17)

Open WebUI, consuming the platform gateway and **nothing else** (§M17).

Not forked, not patched. Everything below is configuration, so upgrading is a digest
bump rather than a merge — which matters because the alternative is carrying a patch set
across an upstream project that moves weekly, on a network that cannot fetch it.

```bash
make chat        # provision credentials if needed, then start
make chat-key    # rotate the gateway credentials
```

---

## Two sites, one port

```
ai-platform.local        admin console + /api/v1 + /v1
chat.ai-platform.local   Open WebUI
```

Routed by **name**, not by path. Open WebUI's built assets reference absolute paths
(`/static/…`), so it can only be served from a site root — `/chat/` would return a page
whose scripts all 404. Giving it a hostname keeps §14's "only nginx is published" intact.

A developer machine usually has no DNS entry for `chat.*`, so nginx also listens on 8081
and `docker-compose.dev.yml` publishes it. The listener exists in both environments;
only the port mapping is development-only, so production still exposes exactly one port.

`chat_proxy.conf` carries the WebSocket upgrade — Open WebUI uses socket.io, and without
it the handshake silently falls back to long-polling, which mostly works and then
intermittently does not.

---

## Who is asking (the interesting part)

Open WebUI holds **one** API key on behalf of every person in the organisation. The naive
integration therefore accounts for every chat in the building under a single identity,
and "who used how many tokens" becomes unanswerable — which is the question a chargeback
report, a quota, or an audit of who asked the model what all need to answer.

So the frontend forwards the signed-in user with each request. That opens the obvious
hole: a forwarded identity is just a string, so anything holding an API key could
attribute its traffic to anyone.

Three mechanisms, strongest first. Implemented in
[`app/services/identity.py`](../backend/app/services/identity.py).

| | Mechanism | Trusted? | Forgeable by |
|---|---|---|---|
| 1 | **Signed JWT** — `X-OpenWebUI-User-Jwt`, HS256, shared secret | yes | someone holding the API key **and** the signing secret |
| 2 | Plaintext header — `X-OpenWebUI-User-Email` | yes | anyone holding that client's API key |
| 3 | The OpenAI-standard `user` body field | **no** | anyone at all |

Both trusted paths are gated on `api_clients.trusted_identity_headers`, which is **off by
default** and granted deliberately, per client. An ordinary developer application never
has it.

**Once a client has a signing secret, plaintext headers from it are ignored entirely.**
Falling back would hand an attacker a way around the signature: omit the signed header.

Mechanism 3 is recorded but never marked trusted. Applications genuinely use `user` for
per-tenant breakdown and it is worth keeping — but a report that added it to the trusted
rows would bill people for traffic anyone could have labelled as theirs. `GET
/api/v1/usage/by-user` groups by trustworthiness as well as by name, so the two can never
merge into one row.

An invalid assertion — expired, wrong issuer, bad signature — is **not** an error. The
request is legitimate; it is served and accounted to the client instead, and the failure
is logged. Refusing the chat because attribution failed would trade a bookkeeping problem
for an outage.

### Provisioning

`make chat-key` creates (or repairs) the `open-webui` API client with
`trusted_identity_headers`, generates a 48-byte signing secret, mints a key, and writes
both into `.env`. The signing secret is Fernet-encrypted at rest, like node agent tokens,
so a database dump alone does not confer the ability to mint identities.

It **revokes the client's previous keys**, which makes it the rotation command as well as
the provisioning one — and is why `make chat` only calls it when `.env` has nothing.
Calling it on every start would invalidate a running instance's credential, and the
symptom is an empty model list rather than anything that says "your key is gone".

> Anything that drops and recreates the database — including
> `alembic downgrade base` — destroys the API key. Re-run `make chat-key` afterwards.

---

## Configuration decisions

Every one of these is a deviation from an Open WebUI default, taken deliberately.

| Setting | Value | Why |
|---|---|---|
| `ENABLE_PERSISTENT_CONFIG` | `false` | Open WebUI otherwise writes its settings to the database on first boot and ignores the environment ever after. An operator would edit `docker-compose.yml`, see nothing change, and have no way to tell why. Off, this file is the single source of truth (Rule 5). |
| `ENABLE_OLLAMA_API` | `false` | §M17: the platform gateway is the only model source. |
| `BYPASS_MODEL_ACCESS_CONTROL` | `true` | Open WebUI keeps its own per-model access list. Leaving it on means two authorisation systems for one resource, maintained separately and diverging quietly. The platform's **aliases** are the control surface: expose a model by pointing an alias at it. |
| `ENABLE_EVALUATION_ARENA_MODELS` | `false` | The arena injects a pseudo-model answering from somewhere nobody registered — on a governed platform, a model that cannot be accounted for. |
| `ENABLE_COMMUNITY_SHARING` | `false` | Sharing uploads the conversation to openwebui.com. Air-gapped, the button can only fail; connected, it exfiltrates. |
| `OFFLINE_MODE` | `true` | Sets `HF_HUB_OFFLINE=1` and stops the version check phoning home (Rule 4). |
| `RAG_EMBEDDING_ENGINE` | `openai` | Embeddings go through the gateway, so document upload never reaches for a sentence-transformers model from HuggingFace. **Needs an EMBEDDING model deployed** — otherwise upload fails. |
| `DEFAULT_USER_ROLE` | `pending` | New accounts wait for an administrator. That is what makes leaving signup open safe on a closed network. Development overrides this to `user`. |
| `DATABASE_URL` | platform Postgres | Its own schema in the platform's database rather than the image's default SQLite: one database to back up (M25), and one that survives a room full of people chatting at once. Created by `make seed`, not by a Postgres init script — those run only on first initialisation, so an existing install would never get it. |

Signup stays open by default because it is how the first administrator is created; new
accounts are `pending` until approved. Turn it off (`OPEN_WEBUI__ENABLE_SIGNUP=false`)
once Phase 6 puts OIDC in front.

---

## What the platform does not do here

* **No SSO between the console and chat.** They are two applications with two sessions.
  Phase 6's OIDC is what unifies them; until then, an operator has two logins.
* **No conversation content leaves Open WebUI.** The platform records that a request
  happened, by whom, against which model, and how many tokens — never the messages.
  Prompt/response capture is Phase 7's tracing concern, and is a decision to take
  explicitly rather than by accident.
* **No per-user model policy.** Every chat user sees every serving alias. If that needs
  to change it belongs in the platform's RBAC, not in Open WebUI's parallel one.
