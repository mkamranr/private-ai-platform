# Architecture

The contract every module is implemented against. Read this, `database.md` and
`api.md` before writing code for any module (spec §26).

---

## 1. What this codebase is

A **control plane**. Not an inference engine, not an agent framework, not a vector
database.

The spec's core principle (§2) is *do not rebuild mature open-source technologies*.
So the custom code here is the registry, scheduler, policy engine, API gateway,
node agent, admin UI and integration layer. vLLM does inference. LangGraph runs
agents. MCP handles tool interoperability. Qdrant does vector search. PostgreSQL
persists. Prometheus/Grafana/Loki/Tempo handle telemetry.

Every time you are tempted to write something an existing component already does,
that is the signal to integrate rather than implement.

---

## 2. The four layers (§28)

```
                        USER EXPERIENCE
                   Open WebUI  |  Admin UI
                               |
                        PLATFORM API
                     FastAPI control plane
                               |
        +----------------------+----------------------+
        |                      |                      |
   COMPUTE LAYER           AI LAYER             DATA LAYER
   Docker                  vLLM                 PostgreSQL
   Node Agent              LangGraph            Qdrant
   GPU / DCGM              MCP                  Valkey
                           Skills, Tools        MinIO
```

The control plane knows how to *manage* these systems. The systems themselves stay
**replaceable**. That property is the single most important thing about this
codebase, and it is why the interfaces in §3 exist before their implementations.

---

## 3. Replaceability boundaries

Defined in `backend/app/core/interfaces/`. Callers depend on the abstraction, never
on a concrete class.

| Interface | Phase 0 status | First implementation | Intended alternatives |
|---|---|---|---|
| `ContainerRuntime` | defined | `DockerContainerRuntime` (P1) | `KubernetesContainerRuntime` |
| `ComputeBackend` | defined | `DockerComputeBackend` (P2) | `KubernetesComputeBackend` |
| `LLMProvider` | defined | `VLLMProvider`, `MockLLMProvider` (P2) | `SGLangProvider`, `OllamaProvider` |
| `VectorStore` | defined | `QdrantVectorStore` (P5) | `PgVectorStore` |
| `AgentRuntime` | defined | `LangGraphAgentRuntime` (P4) | any agent runtime |
| `GpuProbe` | defined | `NvidiaSmi`, `Dcgm`, **`Fake`** (P1) | — |
| `Scheduler` | defined | `SimpleGpuScheduler` (P2) | topology/quota-aware |
| `ToolExecutor` | defined | MCP, REST, OpenAPI, Internal, Database (P4) | — |

Two rules, both machine-enforced by import-linter (`backend/pyproject.toml`):

- Nothing in `app/core/interfaces/` may import an implementation, a vendor SDK, a
  service or a repository. If an interface needs a vendor type, the abstraction is
  leaking.
- The Docker SDK is reachable only from `DockerService` (Rule 7).

`GpuProbe` is **an addition to the spec's list**, and the most operationally
significant one. Without it, nothing GPU-adjacent could be developed or tested on a
machine without an NVIDIA card — which describes the reference development machine.
See §7.

---

## 4. Request flow and layering (Rule 6)

```
HTTP request
   ↓  app/api/v1/*.py        routers — HTTP concerns only
   ↓  app/api/deps.py        dependency injection, authn, authz
   ↓  app/services/*.py      behaviour; no FastAPI imports
   ↓  app/repositories/*.py  the only layer that writes SQL
   ↓  PostgreSQL
```

Enforced, not merely encouraged:

- Routers must not import `app.repositories` or `app.db` directly. They reach both
  *through* `app/api/deps.py`, which is the DI seam.
- Services must not import FastAPI. That is what lets the same service run from an
  APScheduler worker (P1) or the CLI with no request in scope.
- Repositories never commit. The unit of work is the request or the worker task,
  managed by `app/db/session.py::session_scope`. A repository that committed on its
  own would break atomicity across multi-step operations — most importantly
  "reserve the GPUs *and* record the deployment", which must succeed or fail as one.

Run `make lint` to check. The contracts are in `backend/pyproject.toml` under
`[tool.importlinter]`.

---

## 5. Transactions and audit durability

There are **two** audit write paths, and choosing wrongly loses records.

`AuditService.record()` joins the caller's transaction. Use it for actions that
succeeded, so the record and the action commit or roll back together. An audit log
claiming a model was deployed when the transaction failed is worse than no log.

`AuditService.record_independent()` commits in its own session. Use it for
**refusals and failures**, where the surrounding request is about to raise. A failed
login raises `AuthenticationError`; the request transaction rolls back; a
same-transaction audit row would vanish with it. The platform would then record
every successful login and silently discard every failed one — exactly backwards
for a security audit, and a gap nobody notices until an incident review.

Consequence worth knowing: `record_independent` writes a row with a foreign key to
`users.id`, so the referenced user must already be committed. It deliberately
swallows its own exceptions — an audit write must never turn a clean 401 into a 500
— and logs loudly instead.

---

## 6. Authorisation

`require_permission()` in `app/api/deps.py` is the **only** authorisation primitive.
Every mutating route declares one.

```python
@router.post("/models/{model_id}/deploy")
async def deploy(
    user: Annotated[User, require_permission(Permission.MODEL_DEPLOY)],
) -> DeploymentRead: ...
```

- Permissions are named `resource.action` and defined once in
  `app/core/permissions.py`. Routes reference the constants, so a rename becomes an
  import error rather than a route that silently authorises nobody — or everybody.
- Code never branches on a role name. Roles are configuration; permissions are the
  contract. That is what lets an operator define a new role in Phase 6 without a
  code change.
- The user is re-read from the database on every request rather than trusted from
  token claims, so disabling an account or revoking a role takes effect immediately
  instead of whenever the token happens to expire.
- Denials are audited before the 403 is raised, independently of the request
  transaction.

**This is deviation 1 from the spec's phasing.** §19 puts M03 in Phase 6. Building
~18 modules of API surface first and retrofitting authorisation afterwards is how
authorisation gaps ship. The skeleton lives in Phase 0; full OIDC/LDAP, API keys and
the developer portal remain Phase 6.

---

## 7. Fakes are load-bearing

The reference development machine has **no NVIDIA GPU**. vLLM and DCGM cannot run
on it. So:

| Real | Fake | Selected by |
|---|---|---|
| `NvidiaSmiGpuProbe` / `DcgmGpuProbe` | `FakeGpuProbe` — N synthetic A100s with fluctuating telemetry | `GPU__PROBE` |
| vLLM container | `mock-vllm` — OpenAI-compatible, SSE streaming | `MODELS__DEFAULT_RUNTIME` |

This is not a testing convenience bolted on afterwards. It is what makes the §20 MVP
scenario — register node → import model → deploy → create agent → execute → trace —
an **automated end-to-end test runnable on a laptop**. Without that seam, every
GPU-adjacent bug could only be found on the target hardware, and would be found late.

The real test is re-running the same suite on a GPU host with
`GPU__PROBE=nvidia_smi` and `MODELS__DEFAULT_RUNTIME=vllm`. Anything that passes
locally and fails there marks a leak in an interface boundary — precisely the signal
§28 exists to give.

---

## 8. Air-gap discipline (Rule 4)

The spec places packaging in Phase 8. Treating air-gap purely as a Phase 8
deliverable does not work: eight phases of pulling freely from PyPI and Docker Hub
produce floating base tags, unpinned dependencies and Dockerfiles that `apt-get` at
build time — none of which can be bundled without rework.

**This is deviation 2.** Split it: discipline from Phase 0, bundle tooling in Phase 8.

Enforced by `scripts/check_airgap.py`, run as part of `make lint`:

1. `requirements*.txt` fully pinned with `--hash` entries.
2. `requirements*.in` pin every direct dependency with `==`.
3. Dockerfile base images pinned by **digest**, never by tag.
4. Compose images pinned by digest.
5. No OS package installs at image build time.
6. No runtime code path can shell out to a network fetcher.

Adding a dependency means editing `requirements.in`, running `make lock`, and
committing the regenerated lockfile. Never hand-edit a lockfile.

---

## 9. Configuration (M02)

Precedence, highest wins:

```
constructor arg  >  environment variable  >  .env  >  config.yaml  >  field default
```

- `config.yaml` holds non-secret defaults and is committed.
- Secrets come from the environment or `.env`, which is not committed.
- Required secrets have **no default**, so a missing one fails at startup rather
  than at first use. A container that boots and then 500s on the first login is far
  harder to diagnose than one that refuses to start.
- Nothing outside `app/config/settings.py` reads `os.environ`.

Note when writing tests: Compose injects `.env` as real environment variables, which
sit at the *highest* priority. A test asserting yaml precedence must scrub the
ambient environment — see `tests/unit/test_config.py::_isolate`.

---

## 10. Health and readiness

Liveness reports **only** the process. It touches no dependency. If it reported on
Postgres, a database restart would make Docker kill and restart the backend in a
loop while the real fault lay elsewhere, turning a recoverable blip into a
cascading outage.

Readiness probes each dependency **independently and concurrently**, every probe
bounded by a timeout, and reports per-dependency state and latency. A single
aggregate boolean is nearly useless in operation: "not ready" tells an operator
nothing; "postgres ok, redis ok, qdrant timeout after 5s" tells them where to look.
On an air-gapped system where nobody can attach a debugger to production, that
difference decides how long an outage lasts.

Which dependencies are required is configurable (`HEALTH__REQUIRED`), because
readiness requirements change per phase — Qdrant and MinIO only start mattering at
Phase 5.

Health responses are unauthenticated, so they carry dependency names, states and
latencies — never a DSN, credential or stack trace.

---

## 11. Errors

Services raise domain errors (`app/core/errors.py`); they never build HTTP
responses. Mapping to status codes happens in one place, so the same service is
reusable from a worker or the CLI.

An unexpected exception returns an opaque 500. Stack traces, driver messages and SQL
go to the log, never to the client — on an air-gapped security platform an error
body is an information-disclosure surface (§25).

`403` for a missing permission, never `404`: the caller is authenticated and simply
lacks the grant. The response names the required permission so an administrator can
act on it.

---

## 12. Streaming (§25)

The gateway must not buffer an LLM response. Three things have to line up, and
missing any one produces "nothing for 30 seconds, then everything at once":

1. `LLMProvider.chat_stream()` is a separate method, not a flag. Streaming bolted
   onto a blocking interface always buffers somewhere.
2. FastAPI returns a `StreamingResponse`.
3. nginx sets `proxy_buffering off` and `proxy_request_buffering off`
   (`docker/nginx/nginx.conf`). This is the most commonly missed piece.

Token accounting interacts with this: you cannot count tokens in a response you
never buffer. Request vLLM's `stream_options: {"include_usage": true}` and read the
final usage chunk, and handle mid-stream client disconnect so a dropped connection
still records usage. Otherwise streamed traffic silently records zero tokens and
every quota and usage report is quietly wrong.

---

## 13. Secrets at rest

The air-gapped stack has no Vault, but tool credentials (LDAP bind passwords, SQL
credentials) must live somewhere. They are stored in PostgreSQL encrypted with
Fernet under a key mounted from **outside** the database
(`SECURITY__ENCRYPTION_KEY`), so a database dump alone yields nothing usable.

`SecretCipher` is constructed at startup, so a malformed key fails immediately
rather than the first time Phase 4 registers a tool.

Audit metadata passes through `app/services/audit.py::redact`, which recursively
strips password/token/key fields. An audit log is one of the most widely readable
tables in the platform; a captured credential there would be a durable leak.

---

## 14. Deviations from the spec, in one place

| # | Spec says | This codebase does | Why |
|---|---|---|---|
| 1 | Auth/RBAC in Phase 6 | Skeleton in Phase 0 | Retrofitting authorisation across ~18 modules is how gaps ship |
| 2 | Air-gap packaging in Phase 8 | Discipline from Phase 0, tooling in Phase 8 | Unbundleable dependencies accumulate silently |
| 3 | §28 interfaces described | ABCs written before implementations | Empty ABCs cost nothing and prevent the Docker/vLLM dead end |
| 4 | Redis | Valkey | Spec permits either; BSD licensing suits on-premises government deployment better than RSALv2/SSPL. `redis-py` unchanged |
| 5 | `/data/ai-platform` volumes | `${PLATFORM_DATA_ROOT}`, default `./data` | Docker Desktop does not share `/data`; the absolute path fails on a developer machine. Production sets it absolute |
| 6 | Alembic (driver unspecified) | asyncpg, not psycopg2 | A second PostgreSQL driver means another wheel in the bundle for no benefit |
| 7 | §M12 lists `COMMAND`/`PYTHON` tools | Registerable but **disabled** | §25 forbids unrestricted shell execution by agents. See `security.md` |

Additions closing gaps the spec leaves open are in `database.md` §"Gaps closed".

---

## 15. Repository layout

Follows spec §17. `backend/app/` follows §M01.

```
backend/app/
├── main.py            app factory + lifespan
├── config/            settings.py, config.yaml  (M02)
├── api/               deps.py + v1/ routers
├── core/              logging, middleware, errors, security, permissions,
│                      interfaces/            (§28)
├── db/                base.py, session.py, clients/
├── models/            SQLAlchemy ORM — every model re-exported in __init__.py
├── schemas/           pydantic wire contracts
├── repositories/      the only layer writing SQL
├── services/          behaviour
├── workers/           APScheduler jobs (from P1)
└── utils/             cli.py
```

**`app/models/__init__.py` must re-export every model.** Alembic autogenerate
compares `Base.metadata` against the live database; a model that is never imported
is absent from that metadata, so autogenerate will cheerfully emit a migration
DROPping its table. This is the single most destructive mistake available here.

---

## 16. Adding a module

1. Read this file, `database.md`, `api.md`.
2. Implement **only** that module (Rule 1).
3. Ship implementation + unit tests + API tests + migration + docs (Rule 2).
4. Declare dependencies in `requirements.in`, run `make lock` (Rule 3).
5. New external integration → an adapter behind an interface (Rule 8).
6. New config → `app/config/settings.py` + `config.yaml` + `.env.example` (Rule 5).
7. Every route declares `require_permission(...)`; every privileged action records
   an audit entry.
8. `make lint && make test` must pass before moving on.
