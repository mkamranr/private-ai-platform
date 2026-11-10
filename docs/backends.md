# Inference backends and alerts

Expansion phase E1. An **inference backend** is a server that already serves models —
vLLM, Ollama, or any OpenAI-compatible endpoint — and that the platform does not start
itself. Registering it gives the platform a health signal, a model list, and a one-click
way to put its models in the registry.

## Backend types

| type | model list | version | notes |
|---|---|---|---|
| `vllm` | `GET /v1/models` | `GET /version` | a trailing `/v1` on the URL is stripped |
| `ollama` | `GET /api/tags` | `GET /api/version` | |
| `openai_compatible` | `GET /v1/models` | — | a trailing `/v1` on the URL is stripped |

Adapters live in `backend/app/services/inference_backends.py` and implement
`InferenceBackendAdapter` (`backend/app/core/interfaces/inference_backend.py`), which
declares its capabilities so later phases can add deploy/stop/metrics per type.

## Outbound URL policy

Every backend URL passes `core/url_policy.check_outbound_url`, shared with the model
registry: only `http`/`https`; no credentials in the URL; no cloud-metadata or
link-local hosts; no public AI SaaS hosts (the platform is private by design). Redirects
are never followed. Private and loopback addresses are allowed, because that is where
on-prem backends live — from inside a container use the host's network address or
`host.docker.internal`, not `localhost`.

## Health checks and alerts

The worker job `backend_health` checks every enabled backend each
`WORKERS__BACKEND_HEALTH_INTERVAL_SECONDS` (default 60, range 10–3600). A check records
status, latency, version and model count. A failing backend raises one `CRITICAL` alert;
repeated failures increment its `occurrences` rather than creating new rows, and the alert
resolves itself when the backend is healthy again. A deployment that newly becomes
`FAILED` also raises an alert.

Alert states: `ACTIVE` → `ACKNOWLEDGED` → `RESOLVED`. Acknowledge and resolve are audited.
At most one open alert exists per fingerprint (partial unique index).

## Registering discovered models

`POST /backends/{id}/models/register` registers selected models with runtime `ollama` or
`external` and links them via `models.backend_id`. Names already in the registry are
skipped. Calls the gateway makes to these models share one circuit breaker per backend (E2).

## Known limitations

- Since E2 the gateway sends a backend's API key to that backend (and only to it). See
  [gateway-policy.md](gateway-policy.md#backend-api-keys).
- Existing models are not backfilled with a `backend_id`.
- GPU-threshold alerts and the MCP URL check are not wired to the shared policy yet.
