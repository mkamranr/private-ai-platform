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
(`BACKEND_MODEL_LOADED`, `BACKEND_MODEL_UNLOADED`). Loading never downloads: a model that
isn't on the Ollama is refused (*pull it there first*). vLLM serves the one model it was
started with, so it can't load or unload; to run models on GPUs the platform manages,
deploy them from the Model Registry instead.

**Pulling a model onto Ollama.** *Pull a model* in *Models* (or the API) has the Ollama
fetch a model onto its own disk, by the name Ollama pulls it by:

```http
POST /api/v1/backends/{id}/models/pull
{"model": "registry.internal/library/llama3.2:3b", "insecure": false}
```

On an air-gapped site, name a model on an internal registry that mirrors the Ollama
library (`host/namespace/model:tag`); `insecure` allows one served over plain HTTP or with
a certificate the Ollama doesn't trust. The Ollama does the download, not the platform, so
it is the Ollama's network access that counts. The call waits until the whole model is on
disk, which for a large one takes minutes (the console's proxy allows an hour, the call
50 minutes); then load it, or register it with *Register all in catalogue*. Needs
`backend.manage`, audited as `BACKEND_MODEL_PULLED`; 422 with Ollama's reason when the
pull fails (no such model, registry unreachable).

### Load over time, and alerts on it

Each health check of a backend that reports its load (vLLM, Ollama) keeps one sample of
those values in `backend_metric_samples`, so *Models* shows a chart of the last six hours
above the list. Running totals (tokens since start) and byte counts are left out of the
chart; they are still in the samples.

```http
GET /api/v1/backends/{id}/metrics/history?since_minutes=360    (backend.view; 1..10080)
```

Two levels, on the Settings page (*Monitoring*), raise a `WARNING` alert when a backend is
at or over them **in two checks in a row** (one busy minute is not enough), and clear it at
the first check under them:

| Setting | Means |
|---|---|
| `backends.load_alert_requests_waiting` | requests queued on the backend (vLLM) |
| `backends.load_alert_kv_cache_percent` | share of vLLM's KV cache in use |

Both are 0 (off) by default. Samples older than `backends.load_retention_days` (7) are
deleted by the hourly retention job, and with the backend. A backend that is down, or that
reports nothing, keeps no samples.

**A backend's own levels.** A backend serving a busy batch model can be expected to queue
more than an interactive one. *This backend warns at* in its *Models* (vLLM backends), or
the API, sets its own levels over the platform's:

```http
PUT /api/v1/backends/{id}/load-alert-levels   {"requests_waiting": 20, "kv_cache_percent": 0}
```

A level left out comes from the platform's, 0 turns that alert off for this backend, and
an empty body returns it to the platform's. They apply from its next health check.
`backend.manage`, audited as `BACKEND_LOAD_LEVELS_CHANGED`; the backend's
`load_alert_levels` shows them.

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
- Ollama reports no queue or KV cache, so only its models in memory are kept and it
  raises no load alert.
- A pull shows no progress until it finishes, and stops if the request is cut off.
