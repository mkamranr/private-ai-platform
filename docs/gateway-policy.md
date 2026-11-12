# Gateway policy: quotas, concurrency, circuit breakers, fallback

Expansion phase E2. Everything here sits in front of the OpenAI-compatible gateway at
`/v1`. An install with no quota rows and no fallback aliases behaves exactly as before:
the per-key request rate (`rate_limit_per_minute`) is still the only limit until an
operator adds one.

Code: `backend/app/services/gateway_policy.py` (enforcement),
`backend/app/services/quotas.py` (administration), `backend/app/services/gateway.py`
(where each call is admitted, charged and released).

## Quotas

A quota row (`gateway_quotas`) applies to one **scope** and **subject**, and sets any of
these limits:

| limit | meaning | resets |
|---|---|---|
| `tokens_per_minute` | tokens per calendar minute | every minute |
| `daily_tokens` | tokens per UTC day | 00:00 UTC |
| `monthly_tokens` | tokens per UTC month | the 1st, 00:00 UTC |
| `max_concurrency` | requests in flight at once (streams count until they finish) | — |

| scope | subject | notes |
|---|---|---|
| `api_key` | the key's **id** (not its secret or prefix) | |
| `end_user` | a **trusted** forwarded identity, e.g. an email | Self-reported `user` values are never limited, because anyone could type someone else's name and exhaust their budget. |
| `model` | the name of the model that **answers** (not an alias) | Repointing an alias cannot move traffic out from under a model's limit. |
| `team` | the team's **id** (E3) | One budget shared by every key whose API client belongs to the team. See [teams.md](teams.md). |

`*` as the subject means "each one individually, unless it has its own row". A `*` daily
budget on `end_user` gives every chat user their own budget; it is never one shared pool.
A row naming the subject wins over the `*` row.

A call is checked against every quota that applies to it (its key, its key's team, its
trusted end user and its model) and refused with **429** and a `Retry-After` header if any limit is
reached. The error body says which: `details.scope`, `details.quota`, `details.limit`,
and `details.used` for budgets.

### How budgets are counted

- **Checked before the call, charged after it.** A completion's cost is unknown until it
  finishes, so the call that crosses a budget is allowed to finish and the next one is
  refused. The overshoot is at most one request per subject.
- Counters live in Valkey, and **usage records are the source of truth**. Whenever a
  day or month counter is missing (a new day, a Valkey restart, a quota created
  mid-month) it is seeded by summing `usage_records`. Usage already recorded today
  therefore counts toward a daily budget created now.
- Audio counts in the unit its usage row records: seconds for transcription, characters
  for speech.
- **Valkey being down never blocks inference.** Daily and monthly budgets fall back to
  summing usage records directly. Per-minute and concurrency limits are skipped, and a
  warning is logged.

Key and end-user quotas apply to calls through `/v1`. Model quotas also apply to the voice
assistant, which goes through the same gateway methods without an API key. Agents and
document ingestion reach models by another path and are not subject to quotas.

### Concurrency leases

Each in-flight request holds a lease in a Valkey sorted set. A crashed replica cannot
release its leases, so each one lapses after `GATEWAY__CONCURRENCY_LEASE_SECONDS`
(default 1800) instead of permanently shrinking the limit.

## Budget alerts

The budget check (E12, every `workers.budget_check_interval_seconds`, default 300) raises
an in-app alert before a daily or monthly token quota runs out:

- **WARNING** at `gateway.budget_warning_percent` of the limit (default 80, a runtime
  setting);
- **CRITICAL** at 100%. At that point the gateway is already refusing requests.

There is one alert per quota, period and subject. A `*` quota is checked for every subject
that used tokens, except subjects with an enabled quota of their own, which are checked
against that quota instead. Disabled quotas raise nothing. The alert clears itself when
usage drops below the warning share, which happens when the day or month resets or the
limit is raised. Per-minute limits and concurrency change too fast to be worth an alert.

Cost budgets in currency are separate and are described in
[chargeback.md](chargeback.md#budgets-e12).

## Circuit breakers

After `GATEWAY__CIRCUIT_FAILURE_THRESHOLD` upstream failures (default 5) within
`GATEWAY__CIRCUIT_WINDOW_SECONDS` (default 60), a backend's circuit **opens**. For
`GATEWAY__CIRCUIT_OPEN_SECONDS` (default 30) calls to it fail fast with 503 and
`Retry-After`, or go to the alias's fallback, instead of each one waiting out a timeout.
After that, one trial call is let through: a success closes the circuit, and a failure
re-opens it immediately.

- The breaker key is `backend:<id>` for models linked to an inference backend, so every
  model on a dead server trips together. Otherwise it is `deployment:<id>`.
- When a model has several deployments, ones with an open circuit are skipped.
- Breakers are held **in memory per control-plane replica**: each replica learns about a
  dead backend independently, within a handful of failures.
  `GET /api/v1/gateway/circuits` shows the view of the replica that answered.

## Fallback aliases

An alias may name a `fallback_alias_id`. It is **explicit, never inferred**: only an
operator knows whether a smaller model's answer is acceptable. The fallback is used when:

1. the alias's model has nothing serving, or every deployment's circuit is open (any
   call, including streams); or
2. a buffered call (chat without `stream`, embeddings, audio) fails upstream, in which
   case it is retried **once** on the fallback. A stream that has started cannot be
   retried.

It is one hop only: the fallback's own fallback is never followed, so a cycle cannot
loop. The caller still sees the name it asked for. The usage row records
`requested_model` (what was asked for), `model` (what answered) and `fallback_from`
(the alias that could not serve). The fallback must fit its own model quotas.

## Backend API keys

Models linked to an inference backend (`models.backend_id`) are called with that
backend's stored key. The key is sent only when the endpoint is the backend's own URL or
beneath it, the same rule as the hosted-provider key, so a key never reaches a server
that did not issue it. The external-runtime health probe uses the same key.

## Administration

- **Console:** *Usage & Quotas* shows request, token, error and fallback totals; open
  circuits; quotas with how much of today's and this month's budget is used; and usage
  by model, by API key and by end user. *Endpoints & Keys* has a fallback picker for each
  alias.
- **Permissions:** `quota.view` (AI admin, developer, auditor) and `quota.manage` (AI
  admin). Creating, changing and deleting a quota is audited.
- **API:** see [api.md](api.md#gateway-policy--expansion-e2).

## Metrics

- `ai_platform_gateway_quota_rejections_total{scope,quota}`
- `ai_platform_gateway_circuit_opens_total`
- `ai_platform_gateway_fallbacks_total{reason}`, where `reason` is `unavailable` or
  `upstream_error`
