# Private AI Platform --- Product Expansion & Implementation Plan

**Project:** `private-ai-platform`\
**Product direction:** Open-source Private Enterprise AI Control Plane\
**Scope:** Incremental expansion of an existing application\
**Primary deployment:** Self-hosted Docker / Docker Compose\
**Target environments:** Connected, restricted-network, and fully
air-gapped enterprises

## 1. Executive summary

Evolve `private-ai-platform` from a collection of self-hosted AI
services into a unified control plane for enterprises that want to run,
govern, and operate AI on infrastructure they control.

An administrator should be able to register and deploy local models,
publish them through one OpenAI-compatible API, manage model versions
and routing, monitor GPUs and services, build private knowledge bases,
configure MCP tools and agents, manage access and audit records, and
install or upgrade the platform without internet access.

**Product promise:** Your models. Your data. Your infrastructure. Your
control.

### Design principles

-   Private by default: no mandatory external APIs, telemetry, hosted
    control plane, or cloud dependency.
-   Offline-first: installation, operation, model registration,
    ingestion, backup, and upgrade must work in air-gapped environments.
-   Backend agnostic: vLLM, Ollama, and other compatible inference
    servers through adapters.
-   OpenAI compatible: stable API for supported chat, completion,
    embedding, and model-list operations.
-   Composable: optional services and capabilities; a basic installation
    must not require every subsystem.
-   Secure by design: least privilege, team/tenant boundaries, secret
    protection, audit, and explicit tool permissions.
-   Observable: correlate requests across gateway, model, retrieval,
    agent, and tool execution.
-   Docker-first: Docker Compose is the reference deployment. Kubernetes
    is not a prerequisite.
-   Incremental: preserve existing repository functionality and
    architecture where practical.
-   Honest capability reporting: distinguish supported, partial,
    unavailable, and unverified capabilities.

## 2. Users and use cases

  -----------------------------------------------------------------------
  Persona                             Goals
  ----------------------------------- -----------------------------------
  Platform administrator              Install, configure, secure,
                                      upgrade, and monitor the platform

  AI/ML engineer                      Register models, tune inference,
                                      manage deployments

  Application developer               Consume stable OpenAI-compatible
                                      APIs

  Knowledge administrator             Ingest documents and manage
                                      retrieval/access

  Agent developer                     Configure agents, tools, MCP
                                      servers, and workflows

  Security/audit administrator        Manage roles, policies, secrets,
                                      and audit

  Business user                       Use approved assistants and
                                      workflows
  -----------------------------------------------------------------------

Primary use cases: - Host internal LLMs on one or more GPU servers. -
Route application teams to approved model endpoints. - Use a small
Ollama model for development and a vLLM model for production. - Maintain
a private company knowledge base with source citations. - Provide agents
access to approved internal systems. - Require human approval before
sensitive actions. - Diagnose overloaded GPUs, unhealthy endpoints, and
failed deployments. - Upgrade the platform by transferring a verified
offline bundle.

### Non-goals for the initial expansion

-   Kubernetes as a mandatory dependency.
-   Public hosted SaaS.
-   Mandatory paid model APIs or external observability.
-   Unrestricted shell execution by agents.
-   Unreviewed marketplace installation of executable tools.
-   Claims that an LLM judge guarantees factual correctness.
-   Replacing enterprise systems such as GitLab, AD, or ticketing
    systems; integrate through controlled connectors.

## 3. Mandatory existing-project assessment

This is an expansion, not a greenfield rewrite. The coding agent must
inspect the repository before changing code.

Inspect: - Directory/module structure, framework and language
versions. - API routes, schemas, auth, database models, and
migrations. - Existing inference integrations and OpenAI-compatible
endpoints. - Dockerfiles, Compose files, volumes, networks, health
checks, and environment variables. - GPU metrics, logs, monitoring, RAG,
vector DB, embeddings, reranking. - MCP, agent, tool, workflow
implementations. - Frontend pages and reusable components. - Tests, CI,
security posture, exposed ports, secret handling. - Known operational
limitations.

Create `docs/architecture/current-state.md` with: 1. Existing
architecture diagram and service responsibilities. 2. API and database
inventory. 3. Existing UI page inventory. 4. Feature status: Existing /
Partial / Planned / External. 5. Security and operations gaps. 6.
Mapping from existing modules to the target architecture. 7. Migration
and backward-compatibility risks.

Do not rewrite a working module just to match suggested naming. Do not
claim a feature exists until verified in code and tested.

## 4. Target architecture

``` mermaid
flowchart TB
  U[Users and Internal Applications] --> UI[Web Application]
  APP[Internal Apps] --> GW[OpenAI-Compatible Gateway]
  UI --> API[Control Plane API]
  API --> IAM[Identity, RBAC and Policy]
  API --> REG[Model and Endpoint Registry]
  API --> RAG[Knowledge and RAG]
  API --> AG[Agent and Workflow Runtime]
  API --> OPS[Operations and Lifecycle]
  API --> AUD[Audit and Activity]
  GW --> ROUTER[Routing, Quotas and Rate Limits]
  ROUTER --> VLLM[vLLM Adapter]
  ROUTER --> OLLAMA[Ollama Adapter]
  ROUTER --> OTHER[Other Compatible Backends]
  RAG --> Q[Qdrant]
  RAG --> EMB[Embedding and Reranking]
  AG --> MCP[MCP Registry and Tool Gateway]
  API --> DB[(PostgreSQL)]
  API --> REDIS[(Redis / Queue / Cache)]
  OPS --> DOCKER[Docker Engine / Compose]
  OPS --> METRICS[Host and GPU Collectors]
  METRICS --> PROM[Metrics Store]
```

### Logical layers

  -----------------------------------------------------------------------
  Layer                               Responsibility
  ----------------------------------- -----------------------------------
  Web UI                              Console, chat, configuration,
                                      monitoring

  Control plane API                   Users, resources, configuration,
                                      lifecycle

  Model gateway                       Inference API, routing, auth,
                                      quotas, usage

  Model adapters                      Backend-specific health, metadata,
                                      deployment

  RAG service                         Parsing, chunking, embedding,
                                      retrieval, reranking, citations

  Agent runtime                       Agent configuration, tools, memory,
                                      approvals

  MCP/tool gateway                    Discovery, authorization, execution
                                      policies

  Workflow runtime                    Multi-step runs, retries,
                                      schedules, approvals

  Data layer                          Relational metadata, vectors,
                                      cache/queue

  Observability                       Metrics, logs, traces, audit

  Operations                          Install, update, backup, restore,
                                      diagnostics
  -----------------------------------------------------------------------

### Technology defaults

Use existing sound choices rather than replacing them without reason.

  -----------------------------------------------------------------------
  Concern                 Preferred option        Notes
  ----------------------- ----------------------- -----------------------
  Backend                 Existing backend or     Typed schemas
                          FastAPI                 

  Frontend                Existing frontend or    Reusable responsive UI
                          React + TypeScript      

  Main database           PostgreSQL              Users, metadata, jobs,
                                                  audit

  Vector DB               Qdrant                  RAG vectors

  Cache/queue             Redis                   Cache and background
                                                  jobs

  Inference               vLLM, Ollama,           Adapter-based
                          compatible servers      

  Metrics                 Prometheus-compatible   Optional profile
                          collection              

  GPU metrics             NVML / DCGM exporter    Graceful CPU-only
                          where appropriate       fallback

  Parsing                 Local pluggable parsers No remote dependency

  Container lifecycle     Restricted backend      Never expose
                          service                 unrestricted Docker
                                                  socket to browser

  Identity                Local accounts;         External IdP not
                          optional OIDC/LDAP/AD   mandatory

  Secrets                 Docker secrets or       Never log plaintext
                          encrypted local store   
  -----------------------------------------------------------------------

### Docker Compose profiles

-   `core`: API, UI, database, Redis, gateway.
-   `inference`: optional local inference services.
-   `rag`: Qdrant, ingestion workers, embedding/reranking services.
-   `agents`: agent runtime, MCP gateway, workflow workers.
-   `observability`: collectors and optional dashboards.
-   `full`: all supported components.

The minimal platform must start without GPU hardware. Missing optional
services should show an unavailable state, not crash the application.

## 5. Feature catalogue

### 5.1 Unified dashboard

Show: - Registered models and active deployments. - Healthy, degraded,
and unavailable endpoints. - Requests/minute, total requests,
input/output tokens. - TTFT, end-to-end latency, throughput, errors. -
GPU utilization, VRAM, temperature, power where available. - CPU, RAM,
disk, and container health. - Knowledge bases, documents, ingestion
failures. - Agent/workflow runs and failed tool calls. - Recent audit
events, alerts, service dependencies.

Requirements: - Time-range filters and refresh controls. - Metric source
and collection timestamp. - Missing values displayed as unavailable, not
zero. - Multi-host support where collectors allow it. - Real production
data only; clearly marked demo fixtures in development. - Useful empty
states on a fresh installation.

### 5.2 Inference backend management

Initial backends: 1. vLLM. 2. Ollama. 3. Custom OpenAI-compatible
endpoint. 4. Later adapters: llama.cpp server, LocalAI, TGI, or others
after compatibility tests.

Registration fields: - Display name and backend type. - Base URL, host,
and port. - Auth mode and secret reference. - TLS verification and
custom CA. - Connection/request timeouts. - Health-check path if
needed. - Host association, labels, environment. - Enabled/disabled
state.

Features: - Test connection. - Discover models where supported; allow
manual registration otherwise. - Backend version and capabilities. -
Health status and last checked time. - Validate endpoint
configuration. - Configure retry/timeouts. - Disable without deleting
history. - Show dependent deployments and aliases.

Adapter contract (illustrative):

``` python
class InferenceBackend:
    async def test_connection(self): ...
    async def list_models(self): ...
    async def get_model_info(self, model_id): ...
    async def get_capabilities(self, model_id): ...
    async def health_check(self): ...
    async def deploy(self, config): ...
    async def stop(self, deployment_id): ...
    async def get_metrics(self, deployment_id): ...
```

Adapters must declare which operations they support. Do not pretend
every backend can deploy or stop models.

### 5.3 Model registry and catalogue

Metadata: - Display name and canonical ID. - Publisher/provider, family,
architecture. - Task: chat, completion, embedding, reranking, vision,
audio, OCR. - Parameter count and context window when verified. -
Modalities, license, source reference. - Quantization, format, artifact
path. - Backend association, tags, owner, description. -
Verification/approval status and validation date.

Features: - Add manually or discover from backend. - Import metadata
from offline manifest. - Group versions and quantized variants. -
Approved / experimental / deprecated / blocked status. - Team/project
access. - Deployment and alias links. - Checksums and artifact
provenance. - Compare configurations. - Approved local model roots. - Do
not store huge model weights in the platform database or download models
automatically.

### 5.4 Model deployment and lifecycle

Wizard: 1. Select model and verify artifact availability/license. 2.
Select backend and host. 3. Configure runtime: image/version, model
path, served name, tensor parallelism, GPU memory utilization, max
context, batching/concurrency, quantization, safe environment variables
and arguments. 4. Validate resources: GPU/VRAM, conflicting assignments,
ports, mounts. Label estimates as estimates. 5. Review effective
configuration and explicitly confirm.

States: `Draft → Validating → Queued → Starting → Healthy`

Failure/terminal states: `Validation Failed`, `Start Failed`,
`Unhealthy`, `Stopping`, `Stopped`, `Deleting`.

Operations: - Start, stop, restart, logs, health, metrics. - Create new
configuration revision. - Roll back where supported. - Delete while
retaining audit history. - Production approval and dependency view.

Security: - No arbitrary Docker commands from browser. - Lifecycle
actions only through a restricted backend service. - Validate templates,
mounts, ports, images, GPU assignments, and environment variables. -
Audit all privileged operations.

### 5.5 OpenAI-compatible gateway

Supported API subset: - `GET /v1/models` - `POST /v1/chat/completions` -
`POST /v1/completions` where supported - `POST /v1/embeddings` -
Streaming responses using SSE - Separate administrative health/readiness
routes

Features: - API key auth, creation, rotation, expiry, revocation. -
Model aliases mapped to deployments. - Team/project access. - Rate
limits and token/request quotas. - Redacted request metadata logging. -
Streaming passthrough. - Timeout, retry, circuit breaker. - Per-model
concurrency limits. - Usage by key/user/team/model/time. - Explicit
fallback policy. - Correlation IDs and OpenAI-compatible errors. -
Allowed-model scopes and request-size limits.

Routing policies: - Explicit alias. - Priority/fallback. - Least-loaded
only with reliable metrics. - Team-specific. - Development/production
aliases. - Sticky routing only where session semantics require it.

Never silently fall back to a different model unless policy permits it.
Record the actual backend/deployment. Do not claim exact token counts
when they are estimates.

### 5.6 GPU and infrastructure monitoring

Host metrics: - CPU, load, RAM, disk, I/O, network where available. -
Docker container status and resource usage.

GPU metrics: - GPU utilization and memory utilization. - VRAM
used/free. - Temperature, power, power limit. - Fan and encoder/decoder
utilization where supported. - GPU model, UUID, driver/runtime
metadata. - Process-to-GPU mapping where permissions permit.

Inference metrics: - Requests/sec, input/output tokens/sec. - TTFT,
end-to-end latency, queue time if available. - Concurrency, errors,
timeouts. - Per-model and per-deployment utilization.

Features: - Host/GPU inventory, history charts, thresholds, alerts. -
Export metrics endpoint. - Clearly separate host-level and
container-level data. - Collector/agent per host or existing exporter. -
No unrestricted host privileges in control-plane container.

### 5.7 Knowledge bases and RAG

Configuration: - Name, description, owner/team, access policy. -
Embedding model/endpoint. - Vector DB. - Chunk size/overlap and metadata
rules. - Reranker, top-k, similarity threshold. - Hybrid search where
supported. - Token budget, retention, indexing schedule.

Initial file types: PDF, DOCX, TXT, Markdown, HTML, CSV, JSON, PPTX,
images via local OCR, scanned PDFs via OCR.

Ingestion: 1. Upload/select source. 2. Validate type and size. 3.
Checksum and duplicate detection. 4. Parse and extract metadata. 5. OCR
if configured. 6. Chunk. 7. Attach source/page/section/access metadata.
8. Embed and upsert vectors. 9. Persist job status/errors. 10. Publish
only after successful indexing.

Retrieval: 1. Authenticate user. 2. Resolve accessible KBs. 3. Normalize
query. 4. Retrieve candidates. 5. Apply access and metadata filters. 6.
Rerank. 7. Build context within token budget. 8. Generate answer. 9.
Return citations. 10. Record retrieval/latency metadata.

Features: - KB/document management and versioning. - Retry, pause,
delete, re-ingest. - Search playground with chunks and scores. -
Reranker comparison. - Page/section citations. - Query history
controls. - Access-aware retrieval. - Index health. - Test questions and
RAG evaluation: relevance, faithfulness/evidence support, retrieval
precision/recall where ground truth exists.

**Critical:** enforce document-level authorization during retrieval, not
just in the UI.

### 5.8 MCP server and tool management

Registration: - Name, transport, endpoint or local process
configuration. - Auth/secret reference, timeout. - Owner/team, enabled
state. - Allowed agents/users/projects. - Health and discovery
timestamp.

Tool catalogue: - Name, description, input schema. - Server
association. - Risk classification. - Required permission. - Approval
requirement. - Timeout/rate/output-size limits. - Enabled state.

Policies: - Allowlist tools per agent. - Deny dangerous tools. -
Validate input schema. - Restrict network destinations where possible. -
Separate read-only from write-capable tools. - Require approval for
high-impact actions. - Audit caller, redacted arguments, outcome,
duration, approval. - Disable immediately without deleting history.

Treat MCP servers and tool descriptions as untrusted. Never expose raw
credentials to the model. Authorization occurs outside the model.

### 5.9 Agent builder and runtime

Agent configuration: - Name, description, instructions. - Model alias
and generation settings. - Allowed KBs, MCP servers, tools. - Maximum
iterations/runtime/token budget. - Tool timeout and memory policy. -
Human approval policy. - Visibility/access roles. - Version and
published state.

Agent types: - Chat assistant. - RAG assistant. - Tool-using
assistant. - Workflow-driven assistant. - Multi-agent coordinator
(later).

Runtime: - Tool selection/execution, retrieval, session context. -
Short-term memory; optional governed long-term memory. - Structured
output, retries, cancellation. - Step trace, approval pause/resume. -
Authorized export.

Safety: - Treat model output as untrusted. - Validate structured
output. - Model cannot grant itself permissions. - Enforce tool
authorization in runtime. - Limit loops, tokens, runtime, and tool
calls. - Approval for configured high-impact actions. - Defend against
prompt injection in retrieved content. - Scope memory to
user/team/tenant. - Kill switch for agents and tools.

### 5.10 Workflow builder

Initial nodes: - Start/trigger, LLM prompt, KB retrieval, MCP tool,
approved HTTP request. - Condition/branch, transform, human approval. -
Delay/schedule, end/response.

Features: - Visual node editor with typed input/output schemas. -
Versioning, draft/publish/pause/archive. - Manual, API, scheduled, and
configured webhook triggers. - Run history, step trace, retries,
timeouts, failure branches. - Human approval inbox and workflow
permissions.

Start with a persisted deterministic workflow engine. Reuse an existing
mature engine if present; avoid premature distributed-workflow
complexity.

### 5.11 Chat and developer playground

Chat: - Select approved model alias, agent, and KB. - Streaming, stop,
regenerate, copy. - Token/latency metadata when available. - Citations
and safe tool activity summaries. - Error and backend availability
details. - Optional history and policy-controlled export.

Developer playground: - OpenAI-compatible request builder. - Model list,
cURL/Python examples. - Streaming toggle. - API key selector (secret
shown only once). - Redacted request/response inspection. - Latency and
usage summary.

API keys: - Name, owner, expiry, allowed aliases, team/project scope. -
Show secret once. - Rotate/revoke and last-used timestamp. - Never
store/display plaintext after creation.

### 5.12 Identity, RBAC, teams

Authentication: - Local accounts first, secure password hashing and
session management. - Admin-controlled reset and API keys. - Optional
OIDC, LDAP/Active Directory later.

Built-in roles:

  Role              Capabilities
  ----------------- ----------------------------------------
  Super Admin       Full platform administration
  Platform Admin    Services, hosts, deployments, settings
  Model Admin       Model registry and deployment
  Developer         API keys, playground, approved models
  Knowledge Admin   KBs and ingestion
  Agent Developer   Agents, workflows, approved tools
  Auditor           Read-only audit and usage
  User              Approved chat, agents, workflows

Authorization: - Deny by default. - Roles grant permissions; resources
have owner/team scope. - Backend and gateway enforce access; frontend
hiding is not security. - Design for tenant/team isolation even if first
deployment has one organization. - Include tenant, team/project, owner,
visibility, and access policy in relevant records. - Apply scope filters
to every protected-resource query.

### 5.13 Audit and governance

Audit: - Login successes/failures. - User/role changes. - API key
lifecycle. - Backend/model/deployment changes. - Gateway policy
changes. - KB/document operations. - MCP/tool changes and invocations. -
Agent/workflow publication and runs. - Approvals. - Settings, backup,
restore, upgrade.

Record: - Event ID/time, actor, tenant/team. - Action, resource type/ID,
outcome. - Client metadata where appropriate. - Correlation ID. -
Redacted before/after summary and reason.

Governance: - Approved/blocked models and tools. - Data
classification. - Prompt/response retention. - Redaction policies. -
Approval policies. - Usage quotas. - Exportable audit reports.

Application audit should be append-only. Hash chaining or immutable
external storage can be a later enhancement.

### 5.14 Usage and quotas

Track requests, input/output tokens (or labeled estimates),
model/backend, key/user/team/project, latency, errors, and GPU time
where reliable.

Controls: - Requests/minute, tokens/minute. - Daily/monthly budgets. -
Concurrency limits. - Prompt/output size. - Allowed models. - Soft
warnings and hard limits. - Audited admin override.

Label infrastructure cost estimates as estimates. Allow administrators
to configure local rates.

### 5.15 Notifications and alerts

Initial alerts: - Backend unavailable; deployment failed. - GPU
memory/temperature threshold. - Low disk. - Ingestion failure. - MCP
disconnected; tool failure. - Workflow failure; quota reached. - Backup
failed; certificate expiry if monitored.

Start with in-app notifications. Optional SMTP and approved internal
webhooks later. Include severity, time, affected resource, explanation,
link, acknowledge/resolve/mute, and audit history.

### 5.16 Offline install, upgrade, backup, restore

Offline bundle: - Versioned Docker images. - Compose files/profiles. -
Configuration templates and migrations. - Static frontend assets. -
Required wheels/dependencies for any host-side tools. - Optional model
manifests (weights need not be bundled). - Checksums and signed
manifest. - Install/upgrade scripts, SBOM, licenses, release notes.

Install: 1. Build/validate bundle on connected build machine. 2. Export
images/artifacts. 3. Generate checksums and signature. 4. Transfer by
approved media. 5. Verify in isolated network. 6. Load images and
configure paths. 7. Run preflight. 8. Start selected profiles. 9. Apply
migrations. 10. Health-check and show summary.

Upgrade: - Check current/target versions and compatibility. - Verify
disk and backup. - Require confirmation. - Create pre-upgrade backup. -
Apply migrations. - Update containers in controlled order. - Verify
health. - Mark success or provide rollback path. - Audit operation.

Backup: - Platform DB, configuration/policy, KB metadata, supported
Qdrant snapshots, user files/manifests. - Protect encryption keys
separately. - Exclude huge model weights unless selected.

Restore: - Validate manifest and compatibility. - Restore controlled
state. - Verify DB/vector health. - Report missing external model
artifacts. - Audit operation.

Operations: - Diagnostics bundle with secrets redacted. - Config
validation, dependency graph, health/readiness. - Log viewer, disk
usage, backup status, version inventory.

### 5.17 Security hardening

Minimum: - No default production password; first-run admin setup. -
Secure password hashing, sessions, CSRF protections as applicable. - API
key hashing/secure verification storage. - Secrets encrypted at rest or
in supported local secret store. - TLS for UI/API/backend connections;
custom CA support. - Strict CORS, rate and request-size limits. - SSRF
defenses for endpoint registration/connectors. - Safe upload validation
and archive extraction protections. - Least-privilege containers; no
unrestricted Docker socket. - Redact secrets and sensitive prompt data
from logs. - Audit privileged operations. - Pin release
images/dependencies; scan vulnerabilities. - SBOM and license inventory.

Threat model: - Malicious model artifacts. - Prompt injection in
prompts/retrieved documents. - Malicious MCP servers/tools. - SSRF,
cross-team leakage, API key theft. - Agent loops/resource exhaustion. -
Malicious uploads. - Compromised images. - Unauthorized deployment
actions. - Prompt/response exposure through logs.

For each threat, document mitigation, residual risk, and test.

## 6. UI page inventory

Primary navigation: 1. Overview 2. Playground 3. Models: Catalogue,
Backends, Deployments, API Gateway 4. Infrastructure: Hosts, GPUs,
Services, Metrics 5. Knowledge: Knowledge Bases, Documents, Retrieval
Playground 6. Agents: Agent Builder, MCP Servers, Tools, Workflows,
Approvals 7. Usage & Audit: Usage, API Keys, Audit Log, Alerts 8.
Administration: Users & Teams, Roles, Settings, Backup & Restore,
Offline Updates

Required pages:

  -----------------------------------------------------------------------
  Page                                Content/actions
  ----------------------------------- -----------------------------------
  Overview                            KPIs, deployment health, GPU
                                      summary, activity, alerts

  Playground                          Chat, model/agent/KB selector,
                                      citations, tool trace

  Model Catalogue                     Search, filters, metadata,
                                      approval, add/import

  Backends                            Connection test, capabilities,
                                      health

  Deployments                         Status, config, logs, metrics,
                                      lifecycle actions

  Deployment Wizard                   Model, backend, runtime, resource
                                      validation, review

  API Gateway                         Aliases, routing, quotas, keys,
                                      usage

  Hosts                               Host inventory and system metrics

  GPU Detail                          GPU inventory, VRAM, utilization,
                                      temperature, workloads

  Services                            Compose service health, versions,
                                      dependencies, logs

  Knowledge Bases                     Create, access, embedding and
                                      retrieval configuration

  Documents                           Upload, status, metadata, retry,
                                      delete, version

  Retrieval Playground                Query, chunks, scores, reranker,
                                      citations

  Agent List                          Versions, model, access, status

  Agent Builder                       Instructions, model, KBs, tools,
                                      memory, limits, approvals

  MCP Servers                         Register, connect, discover tools,
                                      health

  Tool Catalogue                      Schemas, permissions, risk,
                                      approval, usage

  Workflow Builder                    Graph, node config, test, publish

  Workflow Runs                       Status, trace, retries, approvals

  Approval Inbox                      Pending actions and review context

  Usage                               Requests, tokens, latency, errors,
                                      team/model breakdown

  API Keys                            Create, scope, rotate, revoke,
                                      last-used

  Audit Log                           Filter by actor, action, resource,
                                      time, outcome

  Alerts                              Active/resolved, acknowledge, mute

  Users & Teams                       Create/deactivate users and manage
                                      membership

  Roles & Permissions                 Role definitions and resource
                                      permissions

  Settings                            General, security, retention,
                                      networking, integrations

  Backup & Restore                    Create, list, validate, restore

  Offline Updates                     Import, verify, preflight, upgrade

  Diagnostics                         Health checks, logs, support bundle
  -----------------------------------------------------------------------

UX: - Responsive desktop-first UI and consistent shell. - Environment
label (Production/Lab). - Search/filter long lists. - Confirmation for
destructive/privileged actions. - Loading, empty, error, unavailable,
and denied states. - Progress for long operations; SSE/WebSocket where
appropriate. - Real data only; timestamps and units. - Explain
unavailable capabilities. - Accessible keyboard/focus/contrast. - Mask
secrets; reveal only through explicit secure action.

## 7. Core data model

Reuse current database/migration approach. Logical entities:

`tenants`, `teams`, `users`, `roles`, `permissions`, `user_roles`,
`team_memberships`, `inference_backends`, `backend_capabilities`,
`models`, `model_versions`, `model_artifacts`, `deployments`,
`deployment_revisions`, `model_aliases`, `gateway_api_keys`,
`gateway_policies`, `usage_records`, `hosts`, `gpu_devices`,
`knowledge_bases`, `documents`, `document_versions`, `ingestion_jobs`,
`embedding_profiles`, `retrieval_profiles`, `mcp_servers`, `tools`,
`tool_policies`, `agents`, `agent_versions`, `agent_sessions`,
`agent_runs`, `tool_invocations`, `workflows`, `workflow_versions`,
`workflow_runs`, `approval_requests`, `audit_events`, `alerts`,
`backup_jobs`, `upgrade_jobs`, `platform_settings`.

Scoped resources should include: - `id`, `tenant_id`, optional
`team_id`, `owner_user_id`. - `name`, `description`, `status`. -
`created_at`, `updated_at`, `created_by`, `updated_by`. - `metadata`
JSON only for genuinely flexible metadata.

Use foreign keys/indexes. Define retention and deletion behavior. Do not
replace relational structure with unvalidated JSON blobs. Vector payload
belongs in Qdrant where appropriate; keep relational metadata and
ownership in the platform DB.

## 8. API design

Versioned route groups:

``` text
/api/v1/auth/*
/api/v1/users/*
/api/v1/teams/*
/api/v1/roles/*
/api/v1/backends/*
/api/v1/models/*
/api/v1/deployments/*
/api/v1/gateway/*
/api/v1/hosts/*
/api/v1/metrics/*
/api/v1/knowledge-bases/*
/api/v1/documents/*
/api/v1/retrieval/*
/api/v1/mcp-servers/*
/api/v1/tools/*
/api/v1/agents/*
/api/v1/agent-runs/*
/api/v1/workflows/*
/api/v1/approvals/*
/api/v1/usage/*
/api/v1/audit/*
/api/v1/alerts/*
/api/v1/backups/*
/api/v1/updates/*
/api/v1/diagnostics/*
/v1/models
/v1/chat/completions
/v1/embeddings
```

Conventions: - Typed validation, pagination, consistent
filters/errors. - Correlation ID and authorization on every protected
route. - Idempotency keys for long-running creation operations where
practical. - Job IDs for asynchronous operations. - SSE for deployment
logs, ingestion progress, and long runs. - OpenAPI docs from actual
routes. - Authorization tests for every protected route.

Long-running job pattern: 1. Validate request and permission. 2. Persist
job. 3. Worker performs operation. 4. Persist progress/state. 5. UI
subscribes. 6. Retain final status/errors for diagnosis.

## 9. Service boundaries and reliability

Start as a modular monolith unless the repository already has a sound
service split.

Suggested responsibilities: - `api`: control-plane REST API. - `web`:
frontend. - `gateway`: inference proxy/routing. - `worker`: ingestion,
deployment, backup tasks. - `scheduler`: periodic checks/scheduled
workflows if needed. - `postgres`: metadata. - `redis`: queue/cache. -
`qdrant`: optional vector DB. - `metrics`: optional collectors. -
`host-agent`: optional per-host metrics/controlled operations.

Gateway may be separate for independent scaling/reliability. Do not
create microservices without operational justification.

Reliability: - Health/readiness, graceful shutdown. - Bounded retries
with backoff. - Timeouts and cancellation. - Idempotent jobs where
practical. - Persisted job state and queue recovery. - Migrations,
backpressure, clear degraded-mode behavior. - No infinite retries or
unbounded queues.

## 10. Privacy and data handling

-   Local processing by default.
-   No external telemetry by default.
-   No mandatory cloud API.
-   No runtime model/package downloads.
-   Configurable prompt/response retention.
-   Separate operational metadata from prompt content.
-   Redact secrets and sensitive content.
-   Admin-controlled export/deletion.
-   Access-aware RAG.
-   User/team/tenant isolation for history, memory, agents, and usage.
-   Document outbound network requirements; air-gapped profile must not
    require internet.

## 11. Testing strategy

Unit tests: - Backend adapters and capability mapping. - Routing,
RBAC/scope, API key, quotas. - Chunking and metadata filters. - Tool
schema validation and agent limits. - Workflow transitions, audit
generation, backup validation.

Integration tests: - API/PostgreSQL, gateway/mock OpenAI server. - vLLM
and Ollama adapters. - Qdrant ingestion/retrieval and reranking. - MCP
discovery/invocation. - Worker restart/job recovery. - SSE streaming and
offline startup.

Security tests: - Cross-team/tenant access. - Unauthorized deployment. -
API key scope bypass. - RAG document leakage. - Prompt injection. -
Malicious tool inputs. - SSRF. - Archive traversal/oversized files. -
Secret leakage. - Audit coverage.

End-to-end tests: - First admin setup. - Register backend/model. -
Create deployment. - Gateway call with key. - Ingest document and cited
RAG query. - Register MCP and invoke approved tool. - Create/run
agent. - Human approval. - Backup/restore. - Offline bundle import and
upgrade.

Performance: - Gateway concurrency/latency/streaming. - Routing
overhead. - RAG ingestion/retrieval. - Metrics collector overhead. -
Large UI lists and worker backpressure.

## 12. Release and documentation

Every release includes: - Release notes and supported deployment
matrix. - Compose profiles and environment reference. - API docs,
architecture, security/threat model. - Backup/restore, upgrade/rollback,
offline install guides. - Backend compatibility matrix and known
limitations. - SBOM, license notices, checksums, signed offline bundle.

Suggested docs:

``` text
docs/
  architecture/
    current-state.md
    target-architecture.md
    data-flow.md
    security-model.md
    threat-model.md
  roadmap/
    feature-matrix.md
    implementation-plan.md
    release-plan.md
  product/
    personas-and-use-cases.md
    ui-page-inventory.md
    acceptance-criteria.md
  guides/
    quick-start.md
    docker-deployment.md
    air-gapped-installation.md
    offline-upgrade.md
    backup-restore.md
  reference/
    api.md
    configuration.md
    backend-compatibility.md
    permissions.md
```

## 13. Prioritized implementation roadmap

### Phase 0 --- Repository audit and foundation (P0)

-   Inspect and run current app/tests.
-   Produce current-state architecture.
-   Inventory API, DB, UI, Compose, security, dependencies.
-   Map existing vs partial vs missing.
-   Define feature flags, optional profiles, compatibility/migration
    plan.

**Acceptance:** existing app still starts; baseline tests recorded; no
feature removed; current-state docs verified.

### Phase 1 --- Core control plane and UX (P0)

-   Unified application shell and navigation.
-   Dashboard using real health data.
-   First-run admin setup and local auth if missing.
-   Basic RBAC and permission enforcement.
-   Platform settings.
-   Backend and model registries.
-   Audit foundation.
-   Background jobs and job-status UI.

**Acceptance:** admin can sign in, register/test backend, register
model; APIs enforce permissions; changes create audit events.

### Phase 2 --- Gateway and API management (P0)

-   OpenAI-compatible chat, model listing, embeddings where supported.
-   Streaming.
-   vLLM, Ollama, custom endpoint adapters.
-   Model aliases and routing.
-   API keys, quotas, timeout/retry/health.
-   Usage and correlation records.

**Acceptance:** standard OpenAI SDK calls local model through gateway;
keys scoped/revocable; streaming works; actual backend is recorded; no
external provider required.

### Phase 3 --- Deployment and observability (P0/P1)

-   Deployment wizard and resource validation.
-   Controlled lifecycle service, logs, status.
-   Host/GPU/CPU/RAM/disk/container metrics.
-   Inference metrics and alerts.

**Acceptance:** supported deployments manageable through safe workflow;
job state survives restarts; metrics real/timestamped; CPU-only works.

### Phase 4 --- RAG and knowledge management (P1)

-   KBs, local ingestion/OCR, Qdrant, embeddings, chunking.
-   Reranking, access-aware retrieval, playground, citations.
-   RAG test sets.

**Acceptance:** user ingests document and gets cited answer;
unauthorized chunks never enter context; retry and diagnostics work; no
cloud OCR/embedding required.

### Phase 5 --- MCP, agents, chat (P1)

-   MCP registry, discovery, tool policies.
-   Agent builder/runtime, budgets, trace, chat.
-   Session isolation, approvals, versioning, disable switch.

**Acceptance:** agent uses only allowed tools; tool calls
authorized/audited; runaway loops stop; high-impact actions can require
approval.

### Phase 6 --- Workflows and enterprise identity (P2)

-   Visual workflow builder and run history.
-   Approval inbox, teams/projects.
-   Optional OIDC and LDAP/AD.
-   Quotas, reports, notifications, more connectors.

**Acceptance:** workflow resumes after approval; team isolation tested;
quotas enforced; external identity optional.

### Phase 7 --- Offline operations and release hardening (P0 for air-gapped production)

-   Offline bundle pipeline and signed manifest.
-   Image export/import and preflight.
-   Backup/restore, offline upgrade/rollback.
-   SBOM, security tests, diagnostics, docs.

**Acceptance:** clean install with network disabled; transferred images
start platform; bundle integrity verified; backup/restore tested; no
mandatory runtime internet.

## 14. Feature prioritization

  Feature                             Value    Complexity Priority
  --------------------------- ------------- ------------- ----------
  Dashboard                            High        Medium P0
  Backend registry                     High        Medium P0
  Model registry                       High        Medium P0
  OpenAI-compatible gateway       Very high          High P0
  API key management              Very high        Medium P0
  vLLM/Ollama adapters            Very high        Medium P0
  Basic RBAC                      Very high        Medium P0
  Audit trail                          High        Medium P0
  Offline installation            Very high          High P0
  Deployment lifecycle                 High          High P1
  GPU monitoring                       High        Medium P1
  RAG knowledge bases             Very high          High P1
  Reranking/retrieval trace            High        Medium P1
  MCP registry/policies           Very high          High P1
  Agent builder                   Very high          High P1
  Human approval                       High        Medium P1
  Usage quotas                         High        Medium P1
  Backup/restore                  Very high          High P1
  Workflow visual builder       Medium/high          High P2
  OIDC/LDAP/AD                         High   Medium/high P2
  Multi-host management                High     Very high P2
  Advanced chargeback                Medium        Medium P2
  Large connector catalogue          Medium          High P2

## 15. Recommended first public release

Keep the first release useful but achievable: 1. Unified dashboard. 2.
Local authentication and basic RBAC. 3. vLLM, Ollama, and custom
OpenAI-compatible backend registry. 4. Model registry. 5.
OpenAI-compatible streaming gateway. 6. API key management. 7. Basic
model alias routing. 8. Real service/GPU monitoring where available. 9.
Qdrant KBs with local ingestion and citations. 10. MCP registry and tool
allowlists. 11. Basic agent builder with execution limits. 12. Audit
log. 13. Docker Compose. 14. Offline installation and upgrade
documentation.

Defer visual workflow editor, advanced multi-agent collaboration, broad
connector marketplace, advanced chargeback, and complex multi-host
orchestration.

## 16. Product differentiators

1.  **One gateway for every private model:** stable aliases, keys,
    policies, quotas, health, usage.
2.  **Model operations, not just listing:** artifacts, revisions,
    runtime settings, resource usage, approval.
3.  **Explainable RAG:** inspect retrieved chunks, metadata, reranker
    scores, source pages, and evidence sent to model.
4.  **Governed agents:** exact tool permissions, approval requirements,
    and execution history.
5.  **Air-gapped lifecycle:** verified installation/upgrade bundles,
    backup, diagnostics, reproducibility.
6.  **Unified traces:** correlate gateway, backend, model, RAG, agent,
    MCP, and final response.
7.  **Local enterprise governance:** teams, roles, and policies work
    without cloud services.

## 17. Definition of done

A feature is not complete just because a page or endpoint exists. It
must include: - Backend and frontend implementation where applicable. -
Validation, authorization, and tenant/team scoping. -
Persistence/migrations where needed. - Audit for privileged changes. -
Loading/empty/success/error/denied UI states. - Logs and operational
visibility. - Unit/integration tests. - API/configuration docs. -
Offline validation for new dependencies. - No secrets in logs or source
control. - Backward-compatibility assessment.

## 18. Coding-agent instructions

### Work with the existing repository

1.  Inspect repository, README, deployment, backend, frontend,
    configuration, tests.
2.  Run current application/tests where possible.
3.  Produce current-state assessment before implementation.
4.  Map target features to existing modules.
5.  Reuse working code and preserve APIs.
6.  Avoid broad rewrites or framework changes without a documented
    reason.

### For every phase

1.  Write implementation design and affected-module list.
2.  Identify migrations and compatibility impact.
3.  Implement backend contracts.
4.  Add authorization and audit.
5.  Implement frontend screens.
6.  Add tests.
7.  Update docs and Compose profiles.
8.  Validate from clean Docker Compose.
9.  Test offline-sensitive paths without network.
10. Report changed files, tests, limitations, and next step.

### Hard constraints

-   Docker Compose reference deployment; no Kubernetes requirement.
-   No runtime internet dependency.
-   No mandatory paid APIs/subscriptions.
-   No remote telemetry by default.
-   No automatic model downloads.
-   No unrestricted Docker socket.
-   Model output and MCP descriptions are untrusted.
-   LLM cannot bypass authorization.
-   Never fake GPU metrics, health, metadata, or results.
-   No secrets in frontend bundles/logs/source.
-   Use migrations; do not silently mutate schema at startup.
-   Pin dependencies/images for releases.
-   Optional services must be genuinely optional.
-   Document configuration and safe defaults.

### Required report after each phase

-   Features delivered.
-   Architecture changes.
-   Files/modules changed.
-   Migrations and environment variables.
-   API routes and UI pages.
-   Tests and actual results.
-   Compose changes.
-   Offline compatibility impact.
-   Security considerations.
-   Known limitations and manual verification.
-   Recommended next phase.

### First coding-agent task

**Implement Phase 0 only.** Do not start by generating a new dashboard
or replacing the frontend.

Create: - `docs/architecture/current-state.md` -
`docs/architecture/target-architecture.md` -
`docs/roadmap/feature-matrix.md` - `docs/roadmap/implementation-plan.md`

Then propose a small Phase 1 change that preserves the current
application and adds the highest-value missing foundation.

## 19. Final product recommendation

Build the platform around three connected products:

### AI Operations

Backend management, model registry, deployments, gateway, GPU
monitoring, usage, and alerts.

### Private AI Applications

Chat playground, knowledge bases, RAG, MCP tools, agents, and workflows.

### Enterprise Governance

Identity, RBAC, policies, approvals, audit, backup, offline
installation, and upgrades.

The best first milestone is to make existing model-serving capabilities
manageable through one secure control plane. A strong first release lets
an administrator register a vLLM or Ollama backend, expose a model
through a secured OpenAI-compatible endpoint, see real health and GPU
usage, create a private knowledge base, and publish a governed RAG
assistant --- all from one UI without mandatory external services.
