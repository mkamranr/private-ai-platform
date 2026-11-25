# Gateway policy: quotas, concurrency, circuit breakers, fallback

Everything here sits in front of the OpenAI-compatible gateway at
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
| `team` | the team's **id** | One budget shared by every key whose API client belongs to the team. See [teams.md](teams.md). |

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
assistant, which goes through the same gateway methods without an API key. Its
speech-to-text and text-to-speech calls are recorded for the person speaking and charged
to the **team of the agent** they talk to; that team's budget refuses them only when
`gateway.enforce_platform_budgets` is on, as for the platform's other calls. Agents
and document ingestion reach models by another path; see
[the platform's own model calls](#the-platforms-own-model-calls).

### Concurrency leases

Each in-flight request holds a lease in a Valkey sorted set. A crashed replica cannot
release its leases, so each one lapses after `GATEWAY__CONCURRENCY_LEASE_SECONDS`
(default 1800) instead of permanently shrinking the limit.

## Budget alerts

The budget check (every `workers.budget_check_interval_seconds`, default 300) raises
an in-app alert before a daily or monthly token quota runs out:

- **WARNING** at the quota's warning share: its own *Warn at* percentage if set,
  otherwise `gateway.budget_warning_percent` (default 80, a runtime setting);
- **CRITICAL** at 100%. At that point the gateway is already refusing requests.

There is one alert per quota, period and subject. A `*` quota is checked for every subject
that used tokens, except subjects with an enabled quota of their own, which are checked
against that quota instead. Disabled quotas raise nothing. The alert clears itself when
usage drops below the warning share, which happens when the day or month resets or the
limit is raised. Per-minute limits and concurrency change too fast to be worth an alert.

### Warning share per quota

Each quota can set *Warn at* (1–99%, `warn_percent` in the API). Leave it empty to follow
the platform default. A team that wants notice well before a hard monthly limit can warn
at 50% while every other quota keeps 80%.

### Telling the caller

A gateway response whose daily or monthly budget is at or past its warning share carries
an `X-Quota-Warning` header, so an application can slow down or tell its user before it
starts getting 429s:

```
X-Quota-Warning: api_key; period=monthly; used=85%
```

When several budgets apply (for example the key's and its team's), all of them are listed,
the most used first, separated by commas. Below the warning share there is no header.
Streamed responses carry it too, because it is decided before the stream starts. The
share is the one the call was admitted against, so it does not include the call's own
tokens.

### On the Usage & Quotas page

Each quota shows its warning share and a status: *On*, *Near limit* with the share used,
or *Used up*. The progress bars turn amber at the quota's own warning share. *Warn at…*
changes the share of an existing quota.

Cost budgets in currency are separate and are described in
[chargeback.md](chargeback.md#budgets).

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

## Spreading calls across copies

A model can run as several deployments (copies on different GPUs or nodes). Each call goes
to one of the copies whose circuit is closed, chosen by `gateway.load_balancing` (Settings
page, *Gateway resilience*):

| Value | Picks |
|---|---|
| `least_busy` (default) | The copy with the fewest calls in flight; equally busy copies take turns. |
| `round_robin` | Each copy in turn, whatever its load. |
| `first` | Always the oldest copy; the others only when it fails. The behaviour before E58, useful when debugging one instance. |

- **In-flight counts live in Valkey**, so every control-plane replica sees the same load.
  A call holds a lease on its copy for as long as it runs; a lease left behind by a crashed
  process lapses after `gateway.concurrency_lease_seconds`. If Valkey is unreachable,
  copies still take turns (per control-plane process) and nothing is refused.
- **A failed call moves to another copy.** A buffered call that fails is retried on up to
  `gateway.replica_retries` other copies (default 1) before the alias's fallback. A
  stream that failed **before sending anything** moves as if nothing had happened. One
  that already sent part of its answer is **resumed** (below).
- A retry on another copy is the same call: it is admitted and charged once, and the usage
  record names the copy that answered.
- `ai_platform_gateway_replica_failovers_total{model,kind}` counts moves (`kind` is
  `buffered`, `stream` or `stream_resume`).

### Resuming a broken stream

When a stream breaks after part of the answer has reached the caller (the copy crashed,
was restarted, or the connection dropped), replaying the request elsewhere would send a
second, different answer under the same response. Instead the gateway holds the text sent
so far and asks another copy of the **same model** to *continue* it: the original messages
plus that text as a trailing assistant message, with `continue_final_message` (vLLM and
SGLang; llama.cpp and Ollama continue such a message by themselves). The caller sees one
answer, under the same stream id, with no error.

- Only when the model can resume ([capability](models.md#capabilities) `resume`: vLLM,
  SGLang, llama.cpp and Ollama by default; an `external` endpoint only when declared), and
  only within `gateway.replica_retries`.
- **On the alias's fallback.** With no copy left to resume on, a stream on an alias with
  a fallback carries on there, if the fallback's model can resume too. Another model
  continues the text in its own voice, so the sentence may change register where it
  resumes; the usage row records the fallback's model and `fallback_from`.
- Not after a tool-call delta (half a call can't be continued), the finish, or the usage
  report has been sent.
- The text is held up to `gateway.stream_resume_max_chars` characters (default 32,768);
  a longer answer is not resumed. `0` turns resuming off.
- The resumed call's `max_tokens` is the caller's less the chunks already sent (runtimes
  send about one token per chunk).
- **Usage.** The broken copy never reports its usage. The resumed call reports the text so
  far as prompt tokens and only the rest as completion tokens, so the total covers the
  answer but the split leans to prompt.
- If the copy taking over breaks too, the next one continues, while retries remain; then
  the stream ends with an `upstream_error` event as before.

The continuation is the same model's, but not necessarily the words the broken copy would
have written; a sentence can change direction where it resumes.

Not built: weights per copy, and session affinity (one conversation pinned to one copy
for prefix caching).

## Fallback aliases

An alias may name a `fallback_alias_id`. It is **explicit, never inferred**: only an
operator knows whether a smaller model's answer is acceptable. The fallback is used when:

1. the alias's model has nothing serving, or every deployment's circuit is open (any
   call, including streams); or
2. a buffered call (chat without `stream`, embeddings, audio) fails upstream, in which
   case it is retried **once** on the fallback; or
3. a stream fails: before any text was sent it starts again on the fallback, once no
   other copy is left; after, it is resumed there (see above) if the fallback can resume.

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
- **API:** see [api.md](api.md#gateway-policy).

## Agent calls

An `agent:<slug>` call is admitted against the same quotas as any other call (API key,
team, trusted end user, and the model the agent answers with) once for the whole run,
and holds one concurrency slot until the run ends. It writes one usage row with
`requested_model` = `agent:<slug>`. A key over its budget gets **429** for agent calls too.

## The platform's own model calls

The platform also calls models itself, not through `/v1`. Those calls write
usage rows too, so they appear on the Usage page, in chargeback and in budgets:

| `endpoint` | `requested_model` | when |
|---|---|---|
| `agent.run` | `agent:<slug>` | an agent run started in the console, through `POST /agents/{id}/execute` or by voice: one row per run. Also the part of **any** run that continues after an approval (one row per continuation). |
| `workflow.llm` | `workflow:<name>` | each workflow LLM step that got an answer. A step that then fails (invalid JSON, say) is still recorded: the tokens were spent. |

`model` is the registry model the configured alias or name resolved to, as for gateway
calls. These rows have no API key. They are attributed to the person who started the run
(`user_id`; for schedule and webhook workflow runs, the publisher) and to the **team of
the agent or workflow** (`team_id`, empty for shared ones). They carry no timings.

An `agent:<slug>` call through `/v1` is still recorded once, by the gateway, against the
key; the run does not record it a second time.

Tokens are counted from the run's model responses as they happen, and recorded when the
run's stream ends for any reason: completed, failed, suspended for an approval, or a
client that hung up.

**By default, quotas count these calls but do not refuse them.** The tokens are
charged to the team's and the model's token budgets, so the next `/v1` call sees them,
but an agent run or workflow step is not stopped by a budget. Stopping a workflow halfway
through because a budget ran out is a decision we leave to you: see
[Enforcing budgets on the platform's own calls](#enforcing-budgets-on-the-platforms-own-calls).
API key and end-user quotas do not apply (there is no key).

### Embeddings

The platform's own embedding calls are recorded the same way, one row per call,
with the token count the runtime reported (`completion_tokens` 0):

| `endpoint` | `requested_model` | when | `user_id` | `team_id` |
|---|---|---|---|---|
| `knowledge.ingest` | `kb:<name>` | each batch of chunks embedded while ingesting or re-ingesting a document | the uploader | the base's team |
| `knowledge.search` | `kb:<name>` | `POST /knowledge-bases/{id}/search` | the caller | the base's team |
| `retrieval.playground` | `kb:<name>` | the retrieval playground (one row per base, and again for the comparison run) | the caller | the base's team |
| `agent.retrieve` | `kb:<name>` | an agent run searching its knowledge bases | the person the run is for | the base's team |
| `workflow.retrieve` | `kb:<name>` | a workflow retrieval step | the run's starter | the base's team |
| `rag.eval` | `kb:<name>` | each question of a RAG evaluation run | whoever started the run | the base's team |
| `memory.write`, `memory.search` | `memory` | storing and recalling a memory | the memory's person | the agent's team |
| `knowledge.probe` | the model | the one-string dimension probe when a base is created | the creator | — |
| `platform.embed` | the model | any other embedding (none today); recorded so nothing goes unseen | — | — |

A search over several bases embeds the query once per base (they may use different
models), so it writes one row per base, each charged to that base's team. A failed
embedding writes no row. By default quotas count these tokens like the rows above and
never refuse them, so a document is not left unindexed because a budget ran out. A model
server that reports no usage records 0 tokens, as it would for a `/v1/embeddings` call.

### Reranking

Each rerank is recorded too: one row per call, after the vector search, when a
knowledge base has a reranker (or the playground names one). The endpoint is the
retrieval's own with `.rerank` added:

| `endpoint` | `requested_model` | `user_id` | `team_id` |
|---|---|---|---|
| `knowledge.search.rerank`, `retrieval.playground.rerank`, `agent.retrieve.rerank`, `workflow.retrieve.rerank`, `rag.eval.rerank` | `kb:<names>`, the bases whose passages were scored | as for that retrieval's embeddings | the bases' team when every passage scored came from bases of one team; otherwise none |
| `platform.rerank` | the model | — | — |

One call scores passages from every base searched, so unlike the query embeddings it
cannot be split per base. It is charged to no team when the passages come from bases of
different teams or from shared bases: charging one team for another's passages would be
wrong.

**Tokens are the engine's own count.** vLLM's `/v1/rerank` reports the input tokens it
scored, and the row carries them as `prompt_tokens`. Hugging Face TEI's `/rerank` reports
none: the row is still written, with its latency, person and team, but with 0 tokens
rather than an estimate, and the gateway logs `rerank_tokens_not_reported`. A failed
rerank writes no row; the retrieval falls back to vector order and says so in its trace.
Budgets count these tokens like the embedding rows above.

### Enforcing budgets on the platform's own calls

Turn on `gateway.enforce_platform_budgets` (Settings page, *Gateway*; off by default) to
refuse these calls once a **team** or **model** token budget (per minute, day or month)
is used up, as a `/v1` call is refused. It is checked before each call, so the call that
crosses a budget finishes and the next one is refused. Concurrency limits do not apply.

| Call | When its budget is used up |
|---|---|
| Agent run started in the console or API (`agent.run`) | `POST /agents/{id}/execute` answers **429** with the budget's message and `Retry-After`; no run is created. A run already going is **stopped before its next model call** (see below), and so is a run resumed after an approval |
| Agent through `/v1` (`agent:<slug>`) | refused by the gateway's own admission, as before |
| Workflow LLM step | the step **fails** with the budget's message; the workflow's `error` edges apply as for any failed step |
| Ingestion | the document is marked **FAILED** with the budget's message and retried automatically once the budget resets (see below) |
| Knowledge search, playground, RAG evaluation, workflow retrieval | refused with 429, or the step fails |
| Agent retrieval and memory | the run continues without the retrieved context or memory, as when the embedding model is down |
| Reranking | the results stay in vector order with a warning in the trace, as when the reranker is down |

The setting is read on every call, so switching it on or off takes effect at once.

**Running agents stop at the budget.** An agent run's tokens are recorded when
the run stops, so the budget counters do not see them while it runs. Each model call in
a run is therefore checked before it is made with the run's own tokens so far counted as
used: a run that reaches its team's or model's budget (per minute, day or month) stops
there, **FAILED** with *Stopped before the next model call:* and the budget's message,
and a `RUN_FAILED` event carrying the refusal in its trace. The model call it was about
to make is not made; a tool call the model had asked for is not run. The tokens it did
spend are recorded as usual. A run resumed after an approval is checked before its first
call. `agent:` calls through `/v1` are not stopped: the gateway admitted them against the
key's quotas when they started.

**Refused documents are retried.** A document whose embedding a budget refused
records when to try again: the refusal's `Retry-After` (the next minute, midnight UTC or
the 1st of the month) plus one minute. The document's `retry_at` says when, and the
console shows it under the error. About every 30 seconds the ingestion worker queues every
such document whose time has passed; it is parsed and embedded again from the stored
original. A budget still used up refuses it again with a new time, so it is one check per
document per reset, never a loop. Any other failure is not retried. Re-ingesting it by
hand clears the retry; a document replaced by a newer version is not retried. The retry
is logged (`document_requeued_after_budget_reset`), not audited: nobody asked for it.

## Timings

Every chat call through `/v1` stores its timings on its usage record. The dashboard and
the Usage & Quotas page report percentiles from them:

| Figure | What it measures | Measured on |
|---|---|---|
| **First token** (`ttft_ms`) | From when the call was admitted to the first chunk carrying content or a tool call. A role-only or empty first chunk does not count. | Streamed calls |
| **Generation time** (`generation_ms`) | From the first content chunk to the last. | Streamed calls |
| **Output tok/s** | `(completion_tokens - 1) / generation time`. The first token arrives at the start of the window, so it is left out. Computed at query time from the stored columns. | Complete, successful streams of at least two tokens |
| **Latency** (`latency_ms`, existing) | Whole call, mean and p95. | All calls |

- **Unmeasured is null, not zero.** A buffered call has no visible first token, because
  its "first token" is the whole answer. Its `ttft_ms` and `generation_ms` are NULL, as
  they are for every row written before timings were recorded. Percentiles skip NULLs, so with no streamed
  traffic in a period the figures are `null` and the console shows "—".
  `timed_streams` says how many calls a figure rests on.
- **What is excluded.**
  - Throughput leaves out client disconnects, failed streams and one-token answers.
  - **Agent and voice calls record time to first output**, what the caller waited:
    a streamed `agent:<slug>` call from admission to the first text shown (narration
    such as "Using lookup…", or the answer), and a voice turn's speech row from the end
    of the person's speech to audio ready to play. A buffered `agent:` call has none.
  - **Per-model figures leave those out** (the dashboard's model rows, chargeback by
    model): an agent's first output follows its tool calls and a voice turn's wait spans
    transcription, the agent and synthesis, so neither is the model's own time. Totals
    and per-team, per-key and per-user figures include them. Agent calls have no
    generation time, so they never count in throughput.
- **Clock.** Times are taken in the gateway process, so they include network time to the
  backend but not the client's side of the connection. The clock starts after API-key
  checks and rate limits.
- **Percentiles, not averages of averages.** The dashboard figure is computed over every
  call in the window, not from the per-model rows.

## Metrics

- `ai_platform_gateway_time_to_first_token_seconds{model}` (histogram; streamed calls)
- `ai_platform_gateway_request_duration_seconds{model,surface}` (histogram)
- `ai_platform_gateway_output_tokens_per_second{model}` (histogram; same rule as the
  table above)
- `ai_platform_gateway_quota_rejections_total{scope,quota}`
- `ai_platform_gateway_circuit_opens_total`
- `ai_platform_gateway_fallbacks_total{reason}`, where `reason` is `unavailable` or
  `upstream_error`
- `ai_platform_gateway_replica_failovers_total{model,kind}`, where `kind` is `buffered`,
  `stream` (E58) or `stream_resume` (E66)
