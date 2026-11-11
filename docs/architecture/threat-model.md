# Threat model

Status: E7 (plan §5.17). This document records, for each threat the plan names, what the
platform does about it today, what risk remains, and which test proves the mitigation.
Every claim cites the code that implements it. Where no test exists, the document says
so. A mitigation without a test is a claim, not a control.

Line numbers were current when this document was written (2026-11-11). If one has
drifted, search for the named function.

## Scope and assumptions

- **Deployment.** A single air-gapped site running the compose stack. One published port
  (nginx, `PLATFORM_HTTP_PORT`). GPU nodes run the node agent.
- **Trusted:** the host operating system, the operator with shell access, the Docker
  daemon, the offline bundle's *build* machine, and whoever holds the bundle signing key.
- **Untrusted:**
  - every API caller until authenticated, and after that limited to their permissions;
  - model weights and metadata;
  - uploaded documents;
  - MCP servers and the tool output they return;
  - URLs that users put into workflows, tools and backends;
  - the content of prompts and retrieved passages;
  - an offline bundle until its signature and checksums verify.
- **Out of scope:**
  - physical access;
  - a compromised host kernel;
  - side channels between tenants sharing a GPU;
  - the security of third-party model weights beyond their integrity.

## Summary

| # | Threat | Strongest control | Largest residual risk |
|---|---|---|---|
| 1 | Malicious model artifacts | Read-only mounts, offline engine env, governance block | Pickle formats accepted. `extra_args` reach the engine command verbatim |
| 2 | Prompt injection | Tool grants ∩ user permissions, approval for HIGH risk tools | No injection detection. Retrieved text enters as a system message |
| 3 | Malicious MCP servers / tools | Discovered tools arrive disabled and HIGH risk | No response size cap before JSON parsing. Shared network |
| 4 | SSRF | `check_outbound_url` on backends and workflows | No DNS resolution. Not applied to MCP, REST tools or manual node URLs |
| 5 | Cross-team leakage | `Scope` + `ScopeGuard`, 404 for out-of-scope resources | A shared agent can reference a team's knowledge base |
| 6 | API key theft | Hash-only storage, scopes, revocation, budgets | Rate limit fails open without Redis. Keys have no default expiry |
| 7 | Agent loops / resource exhaustion | `max_iterations`, `MAX_STEPS`, per-node timeouts | No wall-clock limit on agent runs |
| 8 | Malicious uploads | 64 MiB limit, format allowlist, no disk writes | No zip-bomb limit. ElementTree rather than defusedxml |
| 9 | Compromised images | Digest pins, bundle checksums, **bundle signature (E7)** | Platform-built images are tag-referenced. Signing is opt-in per site |
| 10 | Unauthorized deployment actions | Permission per route, socket only on the node agent | The node-agent token is effectively root on the host |
| 11 | Prompt/response exposure through logs | Access log without bodies, audit redaction | No redaction processor in application logs |
| 12 | Ops actions: backup, restore, upgrade (E7) | Split permissions, typed confirmation, audited host runner | Runner token in `.env`. The runner runs as a host user with Docker access |

---

## 1. Malicious model artifacts

**Mitigations**
- `backend/app/services/model_registry.py:140` `_scan_model_directory`:
  - walks the weights directory and records SHA-256 hashes of weight files (`:156`);
  - checksums are opt-in per import (`import_from_disk(verify_checksums=True)`, `:488`);
  - a directory with no weight files is marked UNAVAILABLE (around `:545`).
- `backend/app/services/compute_backend.py`:
  - weights are mounted into the engine container read-only (`:100`);
  - `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` are set, so nothing is fetched from the hub (`:78`).
- `offline/lib.sh` `verify_bundle`: checks bundled model files against the checksums that
  `scripts/build_bundle.py` writes. Since E7 those checksums sit inside a signed manifest.
- `backend/app/services/deployment.py:226` `_check_governance`: BLOCKED and DEPRECATED
  models are never deployed.

**Tests**
- `tests/api/test_models.py::TestModelRegistry::test_import_of_missing_path_marks_unavailable`
- `tests/api/test_models.py::TestModelRegistry::test_registration_requires_register_not_view`
- `tests/api/test_model_governance.py::TestGovernance::test_blocked_and_deprecated_cannot_be_deployed`
- `scripts/phase8_gate.sh`, tamper step.
- **Gap:** no test covers the checksum recording for weights.

**Residual risk**
- Hashes are recorded at import but never compared with an expected value. This is
  inventory, not verification.
- `_WEIGHT_SUFFIXES` (`model_registry.py:58`) accepts pickle formats (`.bin`, `.pt`,
  `.pth`). Loading them in an engine that unpickles can execute code inside the engine
  container.
- `storage_path` is a free host path, mounted into the engine container.
- `metadata.extra_args` are appended verbatim to the engine command (`deployment.py:860` →
  `compute_backend.py:71`). Nothing prevents `--trust-remote-code`. The person who can do
  this needs `MODEL_REGISTER`, so the risk is an insider or a compromised admin account.

## 2. Prompt injection (prompts, RAG content, tool output)

**Mitigations**
- `backend/app/services/tool_pipeline.py:158` `_authorise`:
  - a tool runs only if the agent version grants it **and** the user holds its permission (`:179`, `:188`);
  - permissions are frozen at run start;
  - injected text can steer the model, but it cannot widen what the run is allowed to do.
- `tool_pipeline.py:170` with `settings.py:579`: COMMAND and PYTHON tool types are refused
  at execution.
- `tool_pipeline.py:200`: HIGH and CRITICAL risk tools suspend the run for a person's
  approval (`settings.py:575`).
- `tool_pipeline.py:59,344`: tool output is truncated to 8,000 characters.
- `backend/app/services/retrieval.py:165`: memories are framed as context, not as instruction.

**Tests**
- `tests/api/test_agents.py::TestToolPipeline`:
  - `test_permission_check_is_an_intersection_not_a_union`
  - `test_tool_the_agent_was_not_granted_is_refused`
  - `test_disabled_tool_types_never_execute`
  - `test_high_risk_suspends_rather_than_running`
  - `test_large_results_are_truncated_and_marked`
- `tests/api/test_agents.py::TestApproval::test_approving_requires_tool_approve_not_tool_execute`
- **Gap:** no test exercises injected content itself.

**Residual risk**
- There is no injection detection or content filtering anywhere.
- Retrieved passages are sent as a **system** message (`agent_runtime.py:385`), and
  `retrieval.py:148` does not frame them as untrusted data.
- LOW and MEDIUM risk tools run without a person in the loop. Classifying a tool's risk
  correctly is part of registering it.

## 3. Malicious MCP servers and tools

**Mitigations**
- `backend/app/services/agent_registry.py` `discover`:
  - discovered tools arrive with `enabled=False` at HIGH risk;
  - names are namespaced `server.tool`, and a name already taken is skipped rather than overwritten.
- `agent_registry.py` `import_manifests`: the endpoint is derived as
  `http://mcp-<name>:8000/` and never taken from the manifest.
- `docker-compose.mcp.yml`: no published ports, and the shared data is mounted read-only.
- Tool credentials are Fernet-encrypted (`app/core/security.py:139`) and injected only at
  call time (`tool_executors.py:107`).

**Tests**
- `tests/api/test_agents.py::TestDiscovery`:
  - `test_discovered_tools_arrive_disabled_and_high_risk`
  - `test_a_name_already_taken_by_another_tool_is_skipped_not_inserted`
  - `test_rediscovery_does_not_undo_operator_decisions`
- `scripts/phase4_gate.sh` (around `:215`).

**Residual risk**
- MCP responses are not size-capped before JSON parsing (`tool_executors.py:296`
  `call_mcp`). Truncation happens only afterwards, in the pipeline.
- Tool descriptions and schemas from a server reach the model verbatim, which makes them
  an injection channel.
- MCP containers share the `ai-platform` network with Postgres and Valkey.
- The `mcp-filesystem` image is pinned by tag, not by digest.

## 4. SSRF

**Mitigations**
- `backend/app/core/url_policy.py:73` `check_outbound_url`:
  - allows http(s) only;
  - refuses credentials in the URL;
  - refuses metadata and link-local hosts, and public AI SaaS hosts.
- Where the policy is applied:
  - inference backends (`inference_backends.py:229`);
  - literal workflow URLs (`workflow_definition.py:306`);
  - templated workflow URLs, checked at run time (`workflow_engine.py:589`), with
    `follow_redirects=False` and a body cap (`MAX_HTTP_BODY`).
- `backend/app/core/agent_url.py:49` `validate_agent_url`, used for node enrolment:
  - port allowlist;
  - every resolved address is checked;
  - optional CIDR pin.

**Tests**
- `tests/unit/test_url_policy.py`:
  - `test_refuses_metadata_and_link_local`
  - `test_refuses_public_ai_services`
  - `test_refuses_credentials_in_url`
  - `test_refuses_non_http_or_hostless`
  - `test_public_host_matches_suffix_not_substring`
- `tests/unit/test_agent_url.py` (13 tests), e.g.:
  - `test_every_resolved_address_is_checked_not_just_the_first`
  - `test_allowed_advertise_cidrs_pins_enrolment_to_one_subnet`
- `tests/api/test_backends.py::TestRegistry::test_url_policy_refuses`
- `tests/api/test_workflows.py::TestDefinition::test_literal_urls_are_checked_against_the_outbound_policy`
- `tests/api/test_workflows.py::TestExecution::test_templated_url_is_checked_at_run_time`
- `tests/api/test_node_enrollment.py::TestVerification::test_a_hostile_address_is_refused_and_never_fetched`

**Residual risk**
- `check_outbound_url` does not resolve DNS:
  - a hostname that points at `169.254.169.254` passes;
  - private and loopback addresses are allowed by design, because on-premises services live there.
- **Not applied** to:
  - MCP server registration;
  - REST tool endpoints (`tool_executors.py:128` checks the scheme only);
  - manually registered node `agent_url` values.
- External model endpoints are checked against public hosts but not metadata hosts
  (`model_registry.py:103`).
- DNS rebinding is an acknowledged gap (`agent_url.py:16`).

## 5. Cross-team and cross-tenant leakage

**Mitigations**
- `backend/app/core/scope.py` `Scope`:
  - shared-or-member visibility;
  - `clause()` adds the SQL filter;
  - an out-of-scope resource is a 404, so its existence is not disclosed.
- `backend/app/services/teams.py:319` `ScopeGuard`: per-resource checks. Agents may only
  reference knowledge bases and models visible to their author
  (`api/v1/agents.py:358-362, 415-417`).
- `retrieval.py:107`: cross-tenant knowledge bases are dropped at run time.
- Memory:
  - a tenant is required (`memory.py:79`);
  - anonymous `remember` is refused (`memory.py:336`);
  - anonymous recall returns nothing (`retrieval.py:134`).

**Tests**
- `tests/api/test_teams.py::TestScopeRule`, `TestReassignment`
- `tests/api/test_teams.py::TestIsolation`:
  - `test_lists_hide_other_teams`
  - `test_other_teams_resources_are_not_found`
  - `test_creating_into_a_team_requires_membership`
  - `test_alias_cannot_point_at_another_teams_model`
- `tests/api/test_teams.py::TestGatewayScope`:
  - `test_a_shared_key_cannot_reach_team_resources`
  - `test_another_teams_agent_is_not_found`
- `tests/api/test_workflows.py::TestScope::test_another_teams_workflow_is_invisible`
- `tests/api/test_knowledge.py::TestMemoryScope`
- `tests/api/test_knowledge.py::TestVectorFilterTranslation::test_untranslatable_filters_raise_rather_than_search_unfiltered`

**Residual risk** (from reading the code, not yet covered by a test)
- At run time, retrieval checks the knowledge base's tenant, not its team.
- A team member can create a **shared** agent that references their team's knowledge
  base. Anyone who can see the agent can then query that base through it.
- `tenant_id` defaults to `"default"`, so a deployment is effectively single-tenant
  unless configured otherwise.

## 6. API key theft

**Mitigations**
- `app/core/security.py:181` `generate_api_key`:
  - keys come from `token_urlsafe`;
  - only a SHA-256 hash and a display prefix are stored;
  - comparison uses `hmac.compare_digest`.
- The plaintext key is shown once. Audit records carry the prefix only (`api_keys.py:107`).
- Rotation's grace window can only bring expiry forward (`api_keys.py:160`). Revocation is
  immediate (`api_keys.py:215`).
- `gateway.py`:
  - `authenticate` refuses revoked or expired keys and disabled clients;
  - `check_scope` enforces surface and model scopes;
  - `check_rate_limit` applies a per-key window in Valkey.
- `gateway_policy.py:126` `admit`: token budgets and concurrency limits cap the damage a
  stolen key can do.

**Tests**
- `tests/unit/test_security.py::TestApiKeys`
- `tests/unit/test_api_key_scopes.py`
- `tests/api/test_models.py::TestApiKeys`:
  - `test_key_is_returned_once_and_never_again`
  - `test_key_hash_is_not_the_key`
  - `test_revoked_key_is_rejected_immediately`
- `tests/api/test_gateway_policy.py::TestBudgets`, `TestConcurrency`

**Residual risk**
- No test covers rotation or the per-key rate limit itself.
- The rate limit and the per-minute budget fail **open** when Valkey is down
  (`gateway.py:286`, `gateway_policy.py:222`). This is a deliberate availability choice.
- A key with empty scopes is unrestricted.
- Keys have no expiry by default.

## 7. Agent loops and resource exhaustion

**Mitigations**
- `agent_runtime.py:170`: the loop is bounded by `max_iterations`, and a run that exceeds
  it fails. The value is constrained to 1–100 in the schema and by a database constraint.
- `workflow_engine.py`: `MAX_STEPS=200`.
- `workflow_definition.py`:
  - at most 50 nodes;
  - cycles are refused;
  - per-node timeout capped at 600 s and attempts at 5.
- Each tool has its own timeout (`tool_pipeline.py:235`).
- Gateway budgets and concurrency limits (threat 6).
- Ops jobs (E7) run one at a time (`repositories/ops.py` `claim_next`).

**Tests**
- `tests/api/test_workflows.py::TestDefinition::test_cycles_and_missing_start_are_refused`
- `tests/api/test_gateway_policy.py::TestConcurrency::test_model_concurrency_limit_and_release`
- `tests/api/test_knowledge.py::TestChunking::test_overlap_at_or_above_chunk_size_cannot_hang`
- `tests/api/test_ops.py::TestRunnerProtocol::test_only_one_job_runs_at_a_time`
- **Gap:** no test covers exceeding `max_iterations` or `MAX_STEPS`.

**Residual risk**
- `agents.run_timeout_seconds` is defined but not enforced, so an agent run has no
  wall-clock limit.
- Agent runs and ingestion are not admitted against gateway quotas.

## 8. Malicious uploads

**Mitigations**
- `api/v1/knowledge.py:45`: a 64 MiB upload limit (`MAX_UPLOAD_BYTES`). Empty files are
  refused.
- `document_parsers.py:67` `parse`: format allowlist, and binary content is refused.
- DOCX and PPTX are read with stdlib `zipfile` and ElementTree:
  - only named members are extracted;
  - nothing is written to disk.

**Tests**
- `tests/api/test_knowledge.py::TestParsing`:
  - `test_binary_is_refused`
  - `test_docx_without_lxml`
  - `test_pptx_cites_the_slide`
  - `test_html_drops_script_and_style`
- `tests/api/test_media.py::test_an_empty_upload_is_refused`
- **Gap:** no test covers the size limit.

**Residual risk**
- The whole file is read into memory before the size check.
- nginx sets `client_max_body_size 0`, so the proxy enforces no cap.
- Zip members have no decompressed-size limit, so a zip bomb is possible.
- XML is parsed with stdlib ElementTree rather than defusedxml.
- PDF parsing and OCR are unbounded in pages and time.

## 9. Compromised images and bundles

**Mitigations**
- `docker-compose.yml` pins third-party images by `@sha256`. `scripts/check_airgap.py`
  enforces this in every phase gate.
- `scripts/build_bundle.py` saves images by digest and records each archive's SHA-256.
- `offline/lib.sh` `verify_bundle` checks every archive before anything is loaded.
- **E7: signed manifests.**
  - `scripts/build_bundle.py --sign-key` signs `manifest.json` with ECDSA P-256 (`manifest.sig`).
  - The manifest carries the SHA-256 of every archive and of the SBOM, so one signature covers the whole bundle.
  - `offline/lib.sh` `verify_signature` checks it with the **host's** `openssl` against a public key installed under `<target>/.trust/`. No code from inside the bundle runs before the signature verifies.
  - Once a site has a trust key, an unsigned or wrongly signed bundle is refused.
- **E7: SBOM.** Each bundle carries `sbom.cdx.json` (CycloneDX 1.5) and `licenses.txt`,
  listing every image digest, every pinned Python wheel with its hash and the vendored
  JavaScript.
- `deployment.py:217,742`: an allowlist restricts runtime images on revisions.
- The node agent never pulls (`node-agent/app/runtime/docker.py:259`).

**Tests**
- `tests/api/test_model_governance.py::TestRevisions::test_image_must_be_allowlisted`
- `node-agent/tests/test_api.py::TestContainerCreate::test_missing_image_fails_clearly`
- `scripts/phase8_gate.sh`, tamper step.
- `tests/unit/test_release_tooling.py`:
  - signature round-trip;
  - a tampered manifest is refused;
  - the wrong key is refused;
  - the SBOM lists pinned packages and images.

**Residual risk**
- Platform-built images (`ai-platform/*`) are exempt from digest pinning in
  `check_airgap.py:258`. They are identified by the archive checksum inside the signed
  manifest instead.
- The default `runtime_images` are tags.
- Signing is opt-in. A site that never installs a trust key accepts unsigned bundles with
  a warning, and the Offline Updates preflight reports it.
- The signing key's protection is the build site's responsibility.
- The node agent does not enforce an image allowlist itself.

## 10. Unauthorized deployment actions

**Mitigations**
- Each management route requires its own permission:
  - deploy requires `MODEL_DEPLOY`;
  - approve requires `MODEL_APPROVE`;
  - ops actions are split three ways (threat 12).
- Blocked models are neither deployed nor routed (`deployment.py` `_check_governance`,
  `gateway.py:113`).
- Node agent:
  - bearer token compared in constant time (`node-agent/app/security.py:29`);
  - refuses to control containers it did not create (`node-agent/app/runtime/docker.py:238` `_require_managed`).
- The Docker socket is mounted only into the node agent (and read-only into alloy). The
  control plane never gets it. E7 keeps that true: backups and upgrades go through a host
  runner, not a socket mount.

**Tests**
- `tests/api/test_route_authorization.py::TestEveryRoute`: every non-public route is 401
  without credentials and 403 without permissions.
- `tests/unit/test_permissions_and_audit.py::TestRoleDefinitions`:
  - `test_model_approval_stays_with_model_administrators`
  - `test_separation_of_duty_holds`
- `tests/api/test_model_governance.py::TestGovernance::test_approval_needs_model_approve`
- `tests/api/test_model_governance.py::TestBlockedInGateway::test_blocked_model_is_403_and_hidden`
- `node-agent/tests/test_api.py::TestAuthentication`, `TestManagedLabelGuard`

**Residual risk**
- The managed-label guard can be switched off (`allow_unmanaged_control`).
- The socket mount is read-write.
- `ContainerSpec` accepts any image, any host volume and any ports. Whoever holds a node
  agent token effectively has root on that host.
- A team-scoped deploy requires only visibility of the model.

## 11. Prompt and response exposure through logs

**Mitigations**
- The access log records method, path, status and duration only: no query string,
  headers or body (`core/middleware.py:159`).
- Audit metadata is scrubbed by exact key (`services/audit.py:40,67`). Tool arguments are
  kept out of audit records (`tool_pipeline.py:278`).
- Run events are scrubbed (`agent_runs.py:316`). Secret workflow headers are redacted in
  step traces (`workflow_definition.py:460`).
- Gateway usage rows hold token counts, not content.
- Langfuse is off by default.
- **E7:** `app/core/redact.py` scrubs the diagnostics support bundle:
  - keys containing secret words;
  - `SecretStr` values;
  - credentials in URLs;
  - bearer and basic tokens, JWTs, PEM private keys, API keys;
  - `name=value` secrets in free text.

**Tests**
- `tests/unit/test_permissions_and_audit.py::TestAuditRedaction`
- `tests/api/test_workflows.py::TestDefinition::test_secret_headers_are_redacted`
- `tests/api/test_node_enrollment.py::test_no_credential_reaches_the_audit_log`
- `tests/unit/test_redact.py`
- `tests/api/test_ops.py::TestDiagnostics::test_support_bundle_contains_no_secrets`

**Residual risk**
- `core/logging.py` has no redaction processor, and `logging.redact_headers` is defined but
  unused.
- When Langfuse is enabled, it receives full inputs and outputs.
- `agent_runtime.py:630` logs up to 200 characters of raw tool arguments.
- Some error paths log `str(exc)`.
- Prompts, outputs and tool arguments persist in database rows by design (run history).
  They are protected by permissions, not by redaction.
- Audit key matching is exact-name, so variants such as `x_api_token` slip through. The
  support bundle uses the broader matcher.

## 12. Operations actions: backup, restore, upgrade (E7)

Restoring yesterday's database or installing a bundle is the most destructive thing the
platform can do. Without care, the API that triggers it becomes a path to the host.

**Mitigations**
- **Separate permissions.**
  - `backup.manage` (create, verify), `backup.restore` and `system.upgrade` are distinct.
  - `ADMIN` holds none of them. Only `SUPER_ADMIN` holds all three by default.
  - `diagnostics.view` is read-only and is given to `INFRA_ADMIN` and `AUDITOR`.
- **No new host access for the control plane.**
  - The API only records a job.
  - `scripts/ops_runner.py` runs on the host as an operator-chosen user, and executes the same scripts an operator would run by hand.
  - The backend still has no Docker socket and no `pg_dump`.
- **Runner authentication.**
  - `X-Ops-Runner-Token` is compared with `hmac.compare_digest` against `OPS__RUNNER_TOKEN`.
  - `scripts/gen_secrets.py` generates that token (256-bit).
  - If no token is configured, the runner endpoints are disabled (503).
- **No free-form input reaches a shell.**
  - Names must match `NAME_PATTERN`, and must name an item the runner itself reported in its inventory.
  - The runner passes arguments as a list, never through a shell, and re-checks every name against its own directory listing.
- **Typed confirmation.** Restore, upgrade and rollback need the operator to type the
  target's name, plus a reason.
- **Preflight.** An upgrade is refused while any check fails:
  - signature;
  - format version;
  - a successful verify;
  - free disk;
  - an online runner;
  - no other job in progress.
- **Audit.**
  - `OPS_JOB_REQUESTED`, `OPS_JOB_CANCELLED` and `OPS_JOB_FINISHED` record who, what and why.
  - A restore replaces the database, so the runner re-reports the job afterwards and its outcome is recorded in the restored database.
- **Diagnostics export.** The support bundle is redacted (threat 11), and each export is
  audited as `DIAGNOSTICS_EXPORTED`.

**Tests**
- `tests/api/test_ops.py`:
  - runner authentication;
  - names that are not in the inventory are refused;
  - confirmation is required;
  - preflight blocks an invalid signature;
  - a report recreates a job wiped by restore;
  - permissions are separated.
- `tests/api/test_route_authorization.py`
- `tests/unit/test_release_tooling.py`

**Residual risk**
- The runner token sits in `.env` next to the other platform secrets. Anyone who can read
  `.env` already holds the database password and the encryption key.
- The runner endpoints are reachable on the published port. The token is the only
  barrier, and failed attempts are not rate-limited.
- The runner's host user needs Docker access, which is root-equivalent. Run it under a
  dedicated account, as `deploy/systemd/ai-platform-ops-runner.service` shows.
- A restore is all-or-nothing, and Qdrant vectors are not restored automatically
  (`docs/backup.md`).

## Follow-ups worth scheduling

These are the cheapest residual risks to close, in rough order of value:

1. Apply `check_outbound_url` to MCP server registration, REST tool endpoints and manual
   node URLs.
2. Cap the MCP response size before parsing.
3. Add a decompressed-size limit for DOCX/PPTX members, and set an nginx body limit.
4. Enforce `agents.run_timeout_seconds`.
5. Add a structlog redaction processor, reusing `app/core/redact.py`.
6. Refuse pickle weight formats unless an operator allows them per model.
7. Rate-limit failed runner-token attempts.
