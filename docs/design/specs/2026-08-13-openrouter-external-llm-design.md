# Configurable external LLM (OpenRouter) — design

**Date:** 2026-08-13
**Status:** approved, air-gap implication accepted for now

## Problem

`mock-vllm` returns structured placeholder text, never real completions. Every internal
LLM caller — agents, RAG answer synthesis, skills — therefore exercises the plumbing but
never the model. The user has an OpenRouter API key and wants a configurable endpoint plus
key that the platform calls wherever it needs an LLM.

## What already exists

The work is small because three pieces are already in place:

* An `external` runtime, defined as "the process already exists and the platform only
  points at it. No GPU reservation, no lifecycle" (`settings.py`, `EXTERNAL_RUNTIMES`).
* External deployments already skip node assignment — migration
  `20260808_2034_phase6_external_deployments_have_no_node`.
* `runtime_health_paths["external"] = "/v1/models"`, which is exactly the endpoint
  OpenRouter answers for a model list.

Critically, **every** internal LLM call funnels through one method,
`Gateway._provider(target)` → `VLLMProvider(target.internal_url)`. The agent runtime calls
`provider_for_model()`, which calls the same thing, deliberately: "a second resolution path
would eventually disagree with this one".

The only missing piece is authentication. `VLLMProvider` sends no headers, because it was
written for internal unauthenticated runtimes.

## Design

### 1. Configuration surface

Three fields on `ModelsSettings`, beside the existing `ollama_endpoint` precedent:

| setting | env var | default |
|---|---|---|
| `external_endpoint` | `MODELS__EXTERNAL_ENDPOINT` | `https://openrouter.ai/api` |
| `external_api_key` | `MODELS__EXTERNAL_API_KEY` | empty |
| `external_model` | `MODELS__EXTERNAL_MODEL` | empty |

**Corrected during implementation:** the endpoint is the root, *without* `/v1`. The
provider appends `/v1/chat/completions` itself and the health probe appends `/v1/models`,
so `.../api/v1` produces `/v1/v1/...`. The first `make external-import` failed with
"nothing is answering at https://openrouter.ai/api/v1" for exactly this reason. Ollama's
endpoint is likewise a bare host and port.

Two further corrections the design did not anticipate:

* `wait_until_serving` built its own provider with no credential, so the deployment health
  probe would reject any hosted endpoint that authenticates its model list. It now takes an
  `api_key` and `_attach_external` passes the configured one.
* `Model.served_model_name` resolved external names only for Ollama. Extended to recognise
  an `external://` storage path, so the provider is asked for `vendor/model:free` rather
  than the sanitised platform name.

`external_api_key` is a `SecretStr` so it cannot leak through a repr, log line or
serialised settings dump.

Named `external_*` rather than `openrouter_*` to match the vocabulary the platform already
uses for this class of runtime. OpenRouter is the default value, not a special case: any
OpenAI-compatible endpoint that wants a bearer token works unchanged.

### 2. Auth in the provider

`VLLMProvider.__init__` gains `api_key: str | None = None`. When set, `_client()` sends
`Authorization: Bearer <key>`. When `None` — every existing call site — behaviour is
unchanged.

### 3. One seam

`Gateway._provider(target)` passes the configured key when the resolved target's runtime is
in `EXTERNAL_RUNTIMES`. Agents, RAG, Open WebUI and the `/v1` surface inherit it, because
they all resolve through that method. Token accounting, quotas, per-user attribution and
audit keep working for free, which is the reason for routing through the gateway rather
than calling OpenRouter directly from the agent runtime.

### 4. Registration

A CLI command `external-import`, exposed as `make external-import`, mirroring the existing
`ollama-import`: register the configured model with runtime `external`, create its
deployment, point the default alias at it. Idempotent — re-running is how a changed model
ID is applied.

### 5. Off by default

An empty `MODELS__EXTERNAL_API_KEY` leaves the feature inert, with `mock` still the default
runtime. This is what keeps all ten acceptance gates passing on a machine with no route to
the Internet.

## Air-gap implication

Enabling this makes the platform **not air-gapped**. Outbound HTTPS to openrouter.ai
carries every prompt off the host, including passages retrieved from knowledge bases. For a
defence deployment that is a classification decision, not a technical one. Documented at
the config site and in `docs/airgap.md`; opt-in via an empty default.

`make airgap` is unaffected: it forbids runtime code *shelling out* to network fetchers
(curl, wget, pip), not httpx calls to configured URLs — which is what every provider
already does.

## Testing

* The `Authorization` header is sent when a key is configured, and absent when it is not.
* The key never appears in a `ProviderError` body, a log line, or a settings repr.
* Alias resolution reaches an `external` target and the gateway attaches the key.
* Existing provider tests keep passing unchanged, proving the default is behaviour-preserving.

## Known limitation

The chosen default model, `nvidia/nemotron-3-ultra-550b-a55b:free`, is a free-tier
OpenRouter model and is rate-limited. An agent loop making several calls per run should be
expected to hit 429s. Adequate for verifying the path; not for real work.

## Deviations from the original request

The user asked for "OpenRouter". Delivered as a general authenticated-external-endpoint
setting with OpenRouter as the default value — same code, no extra abstraction, and it does
not need renaming when a second provider appears.
