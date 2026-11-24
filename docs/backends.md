# Inference backends and alerts

An **inference backend** is a server that already serves models —
vLLM, Ollama, or any OpenAI-compatible endpoint — and that the platform does not start
itself. Registering it gives the platform a health signal, a model list, and a one-click
way to put its models in the registry.

## Backend types

| type | model list | version | load now | load / unload | notes |
|---|---|---|---|---|---|
| `vllm` | `GET /v1/models` | `GET /version` | `GET /metrics` | — | a trailing `/v1` on the URL is stripped |
| `ollama` | `GET /api/tags` | `GET /api/version` | `GET /api/ps` | `POST /api/generate` with `keep_alive` | |
| `openai_compatible` | `GET /v1/models` | — | — | — | a trailing `/v1` on the URL is stripped |

Adapters live in `backend/app/services/inference_backends.py` and implement
`InferenceBackendAdapter` (`backend/app/core/interfaces/inference_backend.py`), which
declares its capabilities (`health`, `list_models`, `version`, `metrics`, `deploy`,
`stop`); a backend's `capabilities` list says which it has.

## Load, and loading models

*Models* on a backend's tile shows what it reports about its load above its model list:

- **vLLM**: requests running and waiting, the share of the KV cache in use, and prompt
  and generated tokens since it started, read from its Prometheus `/metrics`, for the
  backend and per model (`model_name`).
- **Ollama**: the models in memory and the GPU memory they use (`/api/ps`).

`GET /backends/{id}/metrics` (`backend.view`) returns the same: `{fetched_at, values,
models}`. A backend type that reports nothing, or a backend that can't be reached, is a
422.

**Ollama can load and unload models.** *Load* puts a model the Ollama already has into
memory and keeps it there, so its first request isn't slow; *Unload* frees its memory (it
stays on disk and loads again on its next request).

```http
POST /api/v1/backends/{id}/models/load     {"model": "llama3.2:latest"}
POST /api/v1/backends/{id}/models/unload   {"model": "llama3.2:latest"}
```

Both need `backend.manage`, an enabled backend, and are audited
(`BACKEND_MODEL_LOADED`, `BACKEND_MODEL_UNLOADED`). Nothing is pulled: a model that isn't
on the Ollama is refused (*pull it there first*), since the platform never downloads from
the Internet. vLLM serves the one model it was started with, so it can't load or unload;
to run models on GPUs the platform manages, deploy them from the Model Registry instead.

## Outbound URL policy

Every backend URL passes `core/url_policy.check_outbound_url`, shared with the model
registry, MCP servers, notification channels and workflow HTTP steps: only `http`/`https`; no credentials in the URL; no cloud-metadata or
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
skipped. Calls the gateway makes to these models share one circuit breaker per backend.

## Known limitations

- The gateway sends a backend's API key to that backend (and only to it). See
  [gateway-policy.md](gateway-policy.md#backend-api-keys).
- Existing models are not backfilled with a `backend_id`.
- Backend load is read when asked, not kept: there's no history and no alert on it.
- GPU-threshold alerts and the MCP URL check are not wired to the shared policy yet.
