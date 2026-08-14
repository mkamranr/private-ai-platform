# Security

Implements the non-functional requirements in §25 and the agent security model in §10.

---

## Non-negotiables (§25)

1. **No secrets in source code.** Enforced by `scripts/check_airgap.py` and by
   required-secret settings having no defaults.
2. **No unrestricted shell execution by agents.** See "Disabled tool types" below.
3. **No unrestricted Docker socket access.** The socket is reachable only from
   `DockerService` (Rule 7, machine-enforced), and never exposed to the control plane
   over an unsecured network (§M04).
4. **No arbitrary tool execution without permission.** Every tool carries an explicit
   permission; agents get no tool by default.
5. **All privileged actions audited.** Both outcomes — including refusals.

---

## Authentication

**Phase 0** local username/password. argon2id via `argon2-cffi` directly rather than
`passlib`, which is effectively unmaintained and whose bcrypt backend detection breaks
against current releases. Parameters are configurable
(`SECURITY__ARGON2_*`); a hash stored under weaker parameters is transparently
upgraded on next successful login.

JWTs via `PyJWT` (python-jose has seen little maintenance). Verified for signature,
expiry, issuer and required claims. A refresh token is rejected where an access token
is expected — type confusion would let a long-lived refresh token call the whole API.
The `alg=none` downgrade is rejected.

Login failures return one message regardless of cause, and hash a dummy password when
the account does not exist so a missing account and a wrong password take comparable
time. Skipping the hash would leak account existence through response latency.

**Phase 6** Keycloak / LDAP / AD / OIDC behind an auth-provider interface. Platform
JWTs remain the internal representation, so nothing downstream changes.

---

## Authorisation

Three levels: user → roles → permissions. Code never branches on a role name.

`require_permission("model.deploy")` is the only primitive. The user is re-read from
the database each request, so revoking a role or disabling an account takes effect
immediately rather than when the token expires.

The eight roles (§M03) are genuinely separated, not near-copies of ADMIN — asserted by
`tests/unit/test_permissions_and_audit.py::test_separation_of_duty_holds`:

| role | can | cannot |
|---|---|---|
| `SUPER_ADMIN` | everything | — |
| `ADMIN` | everything except backup/restore | restore the system of record |
| `AI_ADMIN` | models, deployments, knowledge | create agents, manage infrastructure |
| `INFRA_ADMIN` | nodes, GPUs, containers | create agents, deploy models |
| `AGENT_ADMIN` | agents, skills, tools, MCP | manage infrastructure, deploy models |
| `DEVELOPER` | call APIs, own API keys | manage anything |
| `USER` | chat, execute agents | manage anything, read the audit log |
| `AUDITOR` | read audit, monitoring, traces | change anything |

`tool.approve` is separate from `tool.execute` and granted only to
SUPER_ADMIN/ADMIN/AGENT_ADMIN. Collapsing them would let any tool user approve their
own privileged call, which defeats the §M24 approval workflow entirely.

---

## Secrets at rest

The air-gapped stack has no Vault, but tool credentials must live somewhere.

Fernet-encrypted columns under `SECURITY__ENCRYPTION_KEY`, mounted from **outside**
the database. A database dump alone therefore yields nothing usable — recovering a
credential needs both the dump and the key file.

`SecretCipher` is constructed at startup, so a malformed key fails immediately rather
than the first time Phase 4 registers a tool.

**Key rotation** invalidates every existing ciphertext. Re-encryption tooling is a
Phase 6 deliverable; until then, treat the key as permanent for a deployment and back
it up separately from the database. Storing both in the same backup archive would
undo the whole point.

API keys are stored as SHA-256 plus a visible prefix. The full key is shown once. A
fast hash is correct here — the key is 256 bits of machine-generated entropy, so there
is nothing to brute-force, and gateway auth must not pay an argon2 cost per request.

---

## Audit (§M24)

Every privileged action produces a record: user, action, resource type and id,
timestamp, source IP, result, metadata.

Append-only. No application code updates or deletes an audit row; the repository
exposes no such method, and the absence is the safeguard.

**`DENIED` is distinct from `FAILURE`.** A failure is an error; a denial is an action
understood and refused. A run of denials against one principal is a probing signal and
must be queryable as such.

**Denials and failures are written on an independent transaction.** The request that
triggered them is about to raise and roll back; a same-transaction row would vanish
with it, leaving the platform recording every successful login and silently discarding
every failed one. See `architecture.md` §5.

`source_ip` comes from the request context, not the call site, so no caller can forget
or forge it. `X-Forwarded-For` is honoured **only** when the immediate peer is inside
`SECURITY__TRUSTED_PROXY_CIDRS`; trusting it unconditionally would let any caller forge
the source IP on every audit entry, quietly making the audit log useless.

Metadata passes through `redact()`, which recursively strips password/token/key fields
including nested tool arguments. An audit log is one of the most widely readable tables
in the platform.

---

## Agent security model (§10)

Agents have **no** tool access by default. Every execution passes the full pipeline:

```
agent requests tool
   ↓ permission check    intersection of agent allow-list and invoking user's grants
   ↓ risk check          tool.risk_level
   ↓ approval check      HIGH/CRITICAL suspend the run
   ↓ execute
   ↓ audit
   ↓ return result
```

The permission check is an **intersection**, not a union. An agent must never let a
user reach a tool the user could not call directly — otherwise an agent becomes a
privilege-escalation path, which is the most likely way this platform gets misused.

Approval suspends the run in `WAITING_FOR_APPROVAL`, which can persist across
restarts and deploys. LangGraph's PostgreSQL checkpointer provides the durable
suspend/resume; `AgentRuntime.resume()` therefore takes a `run_id` rather than an
in-memory handle.

### Disabled tool types

§M12 lists `PYTHON` and `COMMAND` among tool types. §25 states plainly that agents get
no unrestricted shell execution. These cannot both hold on the normal execution path,
so the reconciliation is explicit: both types are **registerable but disabled**
(`AGENTS__DISABLED_TOOL_TYPES`).

Shipping them on the normal path would hand any agent with a prompt-injected
instruction a shell on the control plane. Prompt injection is not hypothetical for an
agent whose whole job is reading enterprise documents and ticket text.

Enabling them requires a hardened executor first: separate container, no network,
read-only filesystem, seccomp profile, dropped capabilities, hard timeout, no
credential mounts. Until that exists they stay off.

---

## Transport

**Phase 0** plain HTTP behind nginx, which is the only published port (§14). Postgres,
Valkey, Qdrant and MinIO are reachable solely from inside the `ai-platform` network, so
a misconfigured host firewall cannot expose the database.

**Phase 1** TLS with a platform-generated CA (`scripts/gen_certs.sh`). An air-gapped
site cannot use a public CA — there is no reachable ACME, OCSP responder or CRL
distribution point. Control plane ↔ node agent uses mTLS plus a per-node bearer token,
with node certificates issued `clientAuth`-only so a node certificate cannot
impersonate the control plane.

### Node enrolment (M04)

A node joins by presenting a **one-time enrolment token** an administrator issued for one
specific node name. What that token grants if stolen is deliberately small: the ability to
register **one** node, under **one** pre-agreed name, **once**. It carries no read access
and no control over any existing node.

| | |
|---|---|
| Stored | SHA-256 hash and a display prefix — a database dump yields nothing usable |
| Lifetime | 60 minutes by default (`ENROLLMENT__TOKEN_TTL_SECONDS`) |
| Uses | one, enforced by a compare-and-set in the same transaction that creates the node |
| Revocable | yes, from the console, and revocation is recorded rather than deleted |
| Attempt cap | 5 per token (`ENROLLMENT__MAX_ATTEMPTS_PER_TOKEN`), counted in Postgres |

This is the inverse of how the **agent** token is stored. The platform *verifies* an
enrolment token, so a one-way hash is enough; it *presents* the agent token to the agent on
every poll, so that one must stay reversible and is Fernet-encrypted. Getting the two the
wrong way round fails silently — an encrypted enrolment token still works, it just means a
database read yields live credentials.

**The agent token is generated on the node**, by the install script, and reaches the control
plane only when the node itself sends it. It is never in a rendered page, a download, or an
administrator's clipboard. This also replaces the single fleet-wide `NODE_AGENT_AUTH_TOKEN`
that `.env.example` still describes for the single-host case: every enrolled node gets its
own, so one compromised host does not yield the fleet.

**Enrolment makes the control plane fetch an address the caller supplied**, which is an SSRF
surface. `app/core/agent_url.py` is the boundary. It refuses non-HTTP schemes, embedded
credentials, any path or query, loopback, link-local (including `169.254.169.254`),
multicast and reserved addresses — checking **every** address the name resolves to, not just
the first. The strongest control is the **port allowlist**, default `[9100]`: a node agent
has no business on 22 or 5432. `ENROLLMENT__ALLOWED_ADVERTISE_CIDRS`, set to the GPU subnet,
makes a token stolen out of the building useless.

Private address ranges are **allowed**, against the usual advice, because every node here is
on a private network and blocking them would block the product. The agent's response body is
never echoed back to the caller, so a blind SSRF cannot be read.

Known residual: the address is resolved at validation time and again by the HTTP client at
connect time, so a DNS rebind between the two is not prevented. Pinning the connection to
the resolved address needs a custom transport and is not yet done. The resolved address is
recorded on the enrolment row so the discrepancy is visible afterwards.

> **Named exception: the node agent's port is published without TLS.**
>
> `docker-compose.yml` says a remote node "MUST enable TLS", and the Transport section
> above describes mTLS. The installer shipped in this iteration does **not** provision
> certificates: it publishes 9100 over plain HTTP with bearer-token authentication only.
> On that port the token is the only thing between an attacker on the management network
> and container creation on a GPU host, and it crosses the wire in clear text on every
> 15-second poll.
>
> This is defensible only on a trusted, segmented management VLAN. Mitigations in place:
> `install-node.sh` **refuses a plain-HTTP control plane** unless `--insecure-http` is
> passed, and `--advertise-host` with an IP binds the agent to one interface rather than
> all of them. Wiring mTLS is the next iteration — note that `scripts/gen_certs.sh`
> currently issues node certificates `clientAuth`-only, while the agent is the TLS
> *server* in this relationship, so that script needs a fix first.

Security headers are set on every response (`X-Content-Type-Options`,
`X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`).

---

## Information disclosure

- Unexpected exceptions return an opaque 500. Stack traces, driver messages and SQL go
  to the log only.
- Health responses carry dependency names, states and latencies — never a DSN,
  credential or stack trace. They are unauthenticated.
- Validation errors strip submitted values, so a bad login body never echoes the
  attempted password into a response or a log.
- Interactive API docs are disabled in production: the schema enumerates every
  endpoint and permission, which is reconnaissance on a closed network.
- `/auth/me` never serialises a password hash.

---

## Reliability as a security property (§25)

Health checks, restart policies, timeouts, retries and circuit breakers. The platform
survives individual container restarts.

Startup performs **no** network I/O, which is why a MinIO restart cannot hold the
control plane down. Liveness reports only the process, so a database blip cannot
trigger a restart loop that turns a recoverable fault into an outage.

Every health probe is bounded by a timeout, so a hung dependency cannot hang the health
endpoint itself — which would fail the container's own healthcheck and take the process
down.

---

## Reporting

An air-gapped platform has no vulnerability feed. Dependency review happens when the
offline bundle is rebuilt (Phase 8), against the pinned, hashed lockfile — which is
exactly what makes the inventory auditable: `backend/requirements.txt` is the complete
transitive closure with hashes, so what is installed is knowable without inspecting a
running container.


---

# Authentication providers (M03, Phase 6)

Three ways in, one internal representation. Whoever vouches for a person, the platform
issues **its own JWT** and everything downstream — permissions, audit, the gateway — reads
the platform's `users` row. Adding Keycloak and Active Directory changed no route, no
permission check and no audit record.

| provider | kind | who ever sees the password |
|---|---|---|
| `local` | password | the platform (argon2id hash) |
| `ldap` | password | **only the directory** — the platform binds *as the person* |
| `oidc` | redirect | **only the IdP** — the platform receives a code |

`local` cannot be disabled. An air-gapped site whose IdP is down has no vendor to call and
no second channel; without an account that still works, the platform is unadministrable
exactly when someone needs to fix it.

**`ldap` and `oidc` are both off by default**, so a fresh install signs in with a
username and password form against the platform's own users and nothing else. Directory
sign-in is a configuration decision a site makes when it is ready, not a prerequisite for
running: turning it on later changes no route, no permission check and no audit record,
because every provider resolves to the same platform user and the same JWT.

## Configuration

Active Directory (or any LDAP directory). Enabling it with an incomplete block is refused
at startup — a half-configured provider that fails at the first sign-in attempt instead is
discovered by a user, not by the operator who configured it:

```bash
LDAP__ENABLED=true
LDAP__SERVER_URI=ldaps://dc01.corp.internal:636
LDAP__BIND_DN=CN=svc-ai-platform,OU=Service Accounts,DC=corp,DC=internal
LDAP__BIND_PASSWORD=…
LDAP__USER_SEARCH_BASE=OU=Users,DC=corp,DC=internal
LDAP__USER_FILTER=(sAMAccountName={username})     # uid={username} for OpenLDAP
LDAP__GROUP_SEARCH_BASE=OU=Groups,DC=corp,DC=internal
```

The service account is read-only: it finds the person's DN and reads their groups. The
password itself is checked by **binding as the person**, so the directory stays the only
thing that ever sees it and the platform never holds a hash it would have to protect.

Single sign-on:

```bash
OIDC__ENABLED=true
OIDC__ISSUER=https://keycloak.internal/realms/platform
OIDC__CLIENT_ID=ai-platform
OIDC__CLIENT_SECRET=…

FEDERATION__ROLE_MAPPING={"ai-platform-agent-admins":"AGENT_ADMIN","ai-platform-users":"USER"}
FEDERATION__DEFAULT_ROLES=["USER"]
```

Endpoints and signing keys come from the IdP's discovery document, not from config, so a
key rotation at the IdP needs no platform change — and cannot be missed until every
sign-in starts failing.

## The four federation rules

A directory group is a **claim from outside the platform**. Deciding it *is* a role is the
mistake that makes anyone who can create an AD group a platform administrator. So:

1. **Match on the provider's subject, not the username.** Usernames get reassigned. A new
   joiner inheriting `j.smith` must not inherit the leaver's roles, history and audit
   trail. A username collision with a *different* subject is refused outright — resolving
   it needs a human decision about which name each person keeps.
2. **Never federate over a local account.** A directory entry named `admin` gets a
   refusal, not the break-glass account.
3. **A mapping can never grant SUPER_ADMIN.** That role bypasses every permission check,
   so it stays something an existing administrator confers inside the platform. A group
   that maps to it grants *nothing* — not a substituted default, which would be a role the
   operator never wrote.
4. **Group changes apply on the next sign-in.** Removing someone from a group in AD
   removes their platform access; otherwise the directory stops being authoritative the
   moment it is first read.

All four are tested in `backend/tests/unit/test_federation.py`. None is visible from the
happy path — a site that only tests "an AD user can sign in" passes with every one of them
broken. Rule 1 caught a real bug in this implementation: the "account pre-created by an
administrator" convenience path silently overwrote an existing subject.

## Guards worth naming

**An empty password never reaches the directory.** LDAP treats a simple bind with an empty
password as an *anonymous* bind, which succeeds — so without this, a blank password box
signs you in as any username in the directory, and the platform logs an ordinary
successful login.

**LDAP filter metacharacters are escaped.** `*` as a username matches every entry;
`x)(|(uid=*` rewrites the filter. Injection, reachable from a login form.

**The OIDC `state` is single-use**, stored in Redis and consumed with `GETDEL`. Without it,
an attacker feeds a victim's browser a code they obtained themselves and the platform
issues tokens for the attacker's account — login CSRF.

**The id_token signature is verified** against the IdP's published keys, plus issuer and
audience. Verified end to end against the fixture IdP, including that a token signed with
a key the IdP does not publish is rejected.

**A federated account never gets a password hash.** The null hash is what makes
`POST /auth/login` refuse it, so a directory account cannot become password-signable inside
the platform and outlive the person's removal from the directory.

## Testing SSO without a Keycloak

`fixtures/oidc/` is a real OIDC provider — discovery, authorize, token, JWKS — that signs
tokens with a genuine per-process RS256 key. The signature matters: a fixture returning an
unsigned token would let the platform's verification break with no test noticing, and that
verification is the entire security of the flow.

```bash
docker compose --profile development up -d oidc-fixture
```

**It authenticates nobody.** Any fixture user is issued a token on request, with no
password. `development` profile only.

## The ldap3 trade-off

`ldap3` is quiet upstream, which is the same concern that ruled out `passlib` in Phase 0 —
and the decision lands the other way here, because the alternatives (`python-ldap`,
`bonsai`) are C extensions and an air-gapped bundle must ship wheels built for the target
architecture. Pure Python wins that trade.

It shows: `ldap3 2.9.1` reads pyasn1's pre-0.5 `tagMap`/`typeMap` names, which now emit
`DeprecationWarning`. The import still succeeds and authentication works. The warning is
ignored **scoped to that one module**, so any other deprecation still fails the build.
Pinning pyasn1 back to 0.4.x would have silenced it, at the cost of shipping a 2019 ASN.1
parser — the worse trade.

## What is not built yet

* **LDAP is unverified against a real directory.** The escaping, the empty-password guard
  and the bind mode are tested; the bind itself has never met an Active Directory. Same
  status as the LDAP MCP server.
* **No token revocation store.** `logout` is an audit event; a stolen access token stays
  valid until it expires. The `jti` claim is issued and unused.
* **No account lockout.** `failed_login_count` is maintained but nothing acts on it.


---

# API keys (M20)

Only a **SHA-256 hash and a visible prefix** are stored. The key is shown once, at
creation, and the platform genuinely cannot show it again — which is what stops a database
read from being equivalent to holding every developer's credential. SHA-256 rather than
argon2 is deliberate: the key is 256 bits of machine entropy, so there is nothing to
brute-force, and gateway auth must not pay an argon2 cost on every inference request.

## Scopes

Two independent dimensions, both enforced at the gateway before anything is resolved:

| scope | restricts |
|---|---|
| `chat`, `embeddings`, `models` | which gateway surface the key may call |
| `model:<alias>` | which aliases it may use |

Restricting a dimension is opt-in: a key scoped only to surfaces may call any model, and
one scoped only to models may use any surface. Naming a surface does not implicitly
restrict models to none — a key scoped `["chat"]` would then be unusable, with nothing in
its scope list to explain why.

**Empty means unrestricted.** Every key minted before scopes existed has an empty list, and
reading that as "may do nothing" would take every running integration offline the moment
the platform was upgraded — a total outage caused by a feature nobody opted into.

A refusal names what the key *may* use. The caller holds a valid credential and asked a
reasonable question; a bare denial sends them to an administrator for information the
platform already has.

`/completions` is scoped as `chat`, because it is implemented *through* chat — scoping them
apart would let a key refused for one reach the identical code path via the other.

## Rotation

```
POST /api-keys/{id}/rotate   {"grace_hours": 24}
```

Mints a replacement carrying the same scopes, rate limit and expiry, and puts the **old**
key on a timer rather than revoking it. Rotation with no overlap breaks every integration
still holding the old key at that instant, which is exactly why rotation gets postponed
indefinitely; a grace window means rotate first, redeploy afterwards. `grace_hours: 0` is
available for a compromised key, where breaking callers is the point.

The old key's expiry only ever moves **earlier** — rotating a key that already expires
sooner must not extend its life. `rotated_to` records the successor, so an operator seeing
traffic on a key that should be dead can tell "still rotating" from "someone is using a
credential we retired".

## The developer portal

Served at `/developer/` — the same origin as the API, so its fetches are same-origin and
no CORS configuration has to exist for it to work.

Deliberately a different thing from the admin console, not a page inside it. The audience
wants to call the API, not run the platform: they get a key, a base URL, the aliases they
can call, and a snippet that works. Everything an operator needs is absent, because a
control someone cannot use reads as a permissions bug rather than as a boundary.

---

# Audit (M24)

`GET /api/v1/audit` with filters on user, action, resource, result and date, plus
`GET /audit/actions` for what is *actually* in the log — read from the data rather than the
`AuditAction` enum, because offering a reviewer forty filters that all return nothing is
worse than the eight that will not.

**Read-only by construction.** There is no route that edits or deletes an audit row; every
write verb returns 405 from routing, not from a check someone can forget. The query path
takes the *repository*, not `AuditService`, so the endpoint cannot even reach `record()` —
a query surface that can write to the log it is querying is a hole nobody needs.

Retention is a deliberate database operation (M25), not an API call.

The page shows the total alongside the rows: without it a reviewer cannot tell "this is
everything that matched" from "the page limit truncated it", and on an audit log those are
very different conclusions.

---

# Backup and restore (M25)

```bash
make backup                      # postgres + qdrant snapshots + minio + manifest
make backup-verify B=backups/…   # prove it is restorable
make backup-restore B=backups/…  # replace this platform's data
```

Host-side, like `check_airgap.py`: `pg_dump` lives in the postgres image and Rule 4 forbids
`apt-get` at build time, so the control plane cannot dump its own database — it orchestrates
`docker compose exec` instead.

**Verify proves restorability, not presence.** It re-hashes every artifact *and* asks
`pg_restore` to read the dump's table of contents. A verify that only checks the files
exist is why people discover their backups are empty during an incident.

**The encryption key is not in the backup**, and neither is `.env`.
`SECURITY__ENCRYPTION_KEY` decrypts every tool credential and node token in the dump;
storing it alongside would mean whoever walks off with the archive owns the site's AD bind
password. A *fingerprint* goes in the manifest instead, and **restore refuses** against a
platform holding a different key — because restoring with the wrong one does not fail
loudly, it produces credentials that decrypt to garbage discovered days later.

Qdrant snapshots are taken but not auto-restored: vectors are re-derivable from the chunk
text in PostgreSQL, so they are an optimisation rather than the source of truth.

One thing worth knowing: `pg_restore` cannot read a custom-format dump from a **pipe** — it
fails with "input file does not appear to be a valid archive", which reads exactly like a
corrupt backup and would send someone hunting a data-loss incident that never happened.
Both verify and restore stage the dump to a file inside the container first.
