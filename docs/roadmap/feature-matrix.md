# Feature matrix

Each expansion-plan feature (§5, §14) with its status in this repository. Statuses are
defined in [`../architecture/current-state.md`](../architecture/current-state.md). Paths
are relative to `backend/app/` unless they start with a top-level directory.

| # | Feature | Plan priority | Status | Where | Gap to close |
|---|---|---|---|---|---|
| 1 | Unified dashboard | P0 | Existing (E8) | `api/v1/dashboard.py`, `services/dashboard.py`, `repositories/dashboard.py`, `frontend/admin` | TTFT and throughput (not recorded yet) |
| 2 | Backend registry | P0 | Existing (E1) | `services/inference_backends.py`, `api/v1/backends.py`, `core/url_policy.py` | Gateway use of backend API keys (E2); deploy/stop/metrics capabilities |
| 3 | Model registry | P0 | Existing (E3, E4) | `models/models_registry.py`, `services/model_registry.py`, [model-governance.md](../model-governance.md) | Grouping is by `family`, not a `model_versions` table (deliberate) |
| 4 | OpenAI-compatible gateway | P0 | Existing | `api/v1/gateway.py`, `services/gateway.py`, `services/gateway_policy.py` | Round-robin across replicas; mid-stream failover |
| 5 | API key management | P0 | Existing | `services/api_keys.py`, `frontend/developer` | Expiry and allowed-alias scopes: verify against `test_api_key_scopes.py` |
| 6 | vLLM / Ollama adapters | P0 | Existing | `services/llm_provider.py`; `ollama` is an external runtime | Declared-capability contract per adapter |
| 7 | Basic RBAC | P0 | Existing (E3) | `core/permissions.py`, `models/auth.py`, `services/seed.py` | Mapped to the plan's 8 roles in E3 (`KNOWLEDGE_ADMIN` added); see `docs/teams.md` |
| 8 | Audit trail | P0 | Existing | `models/audit.py`, `services/audit.py` | Export, correlation-ID filter in UI |
| 9 | Offline installation | P0 | Existing (E7) | `offline/`, `scripts/build_bundle.py`, `scripts/bundle_signing.py`, `scripts/sbom.py`, Offline Updates page | Key rotation and revocation |
| 10 | Deployment lifecycle | P1 | Existing (E4) | `services/deployment.py`, `workers/deployments.py`, `services/scheduler.py` | Zero-downtime (blue/green) revisions need replicas |
| 11 | GPU monitoring | P1 | Existing | `node-agent/`, `models/infrastructure.py` | Thresholds feeding alerts |
| 12 | RAG knowledge bases | P1 | Existing (E5) | `services/knowledge.py`, `retrieval.py`, `workers/ingestion.py` | Per-document ACL, whole-base re-embed |
| 13 | Reranking / retrieval trace | P1 | Existing (E5) | `services/knowledge.py` (`retrieve`), `services/rag_eval.py`, `api/v1/retrieval.py` | Answer faithfulness scoring (judge model), query history |
| 14 | MCP registry / policies | P1 | Existing | `models/agents.py`, `services/tool_pipeline.py`, `mcp/` | Shared SSRF policy, MCP Servers page |
| 15 | Agent builder | P1 | Existing | `services/agent_registry.py`, `agent_runtime.py`, `agents/*.yaml` | Global kill switch |
| 16 | Human approval | P1 | Existing (E6) | `agent_runs` approval state, `workflow_approvals`, Approvals page (agent and workflow steps) | none |
| 17 | Usage quotas | P1 | Existing (E2, E3) | `gateway_quotas`, `services/gateway_policy.py`, Usage & Quotas page | Soft warnings at a budget threshold |
| 18 | Backup / restore | P1 | Existing (E7) | `scripts/backup.py`, `services/ops.py`, `scripts/ops_runner.py`, Backup & Restore page | Scheduling, retention, partial restore |
| 19 | Workflow visual builder | P2 | Existing (E6) | `services/workflow_engine.py`, `services/workflows.py`, `workers/workflows.py`, Workflows page | Drag-and-drop canvas, typed node schemas, parallel branches |
| 20 | OIDC / LDAP / AD | P2 | Existing | `services/auth_providers.py`, `fixtures/oidc` | none |
| 21 | Multi-host management | P2 | Existing | node enrollment, `services/node_enrollment.py` | none |
| 22 | Advanced chargeback | P2 | Existing (E9) | `services/chargeback.py`, `api/v1/chargeback.py`, Chargeback page | Currency budgets and cost alerts; non-gateway costs (ingestion, tools, voice) |
| 23 | Large connector catalogue | P2 | Existing (E10) | `mcp/manifests/` (7), `services/connector_catalogue.py`, `services/connector_manifest.py`, Connector Catalogue page | First-party connectors (read-only SQL, HTTP/OpenAPI with a destination allow-list) |
| 24 | Teams / tenant isolation | (5.12) | Existing (E3) | `models/teams.py`, `core/scope.py`, `services/teams.py`, Teams page | Memory/conversations still scoped by tenant and end user, not team |
| 25 | In-app alerts | (5.15) | Existing (E1) | `services/alerts.py`, `api/v1/alerts.py`, Alerts page | GPU-threshold alerts, notification channels |
| 26 | Settings page | (§6) | Existing (E8) | `services/runtime_settings.py`, `api/v1/settings.py`, Settings page | More runtime settings as consumers move to per-request reads |
| 27 | Diagnostics bundle | (5.16) | Existing (E7) | `services/diagnostics.py`, `core/redact.py`, Diagnostics page | none |
| 28 | Speech, OCR, voice | (beyond plan) | Existing | `services/voice.py`, `media_provider.py` | none |

**Summary:** of the 23 features in plan §14, all 23 are Existing.
Every P0 item is Existing.
