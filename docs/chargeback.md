# Chargeback

The console's **AI Models → Chargeback** page, and `/api/v1/chargeback`, turn what the
platform already records into estimated cost per team, API key, platform user, end user or
model. Nothing new is collected: reports read `usage_records` (one row per gateway call,
plus one per agent run and workflow LLM step the platform makes itself, and
one per embedding call for ingestion, search, retrieval and memory)
and `gpu_allocations` (which GPUs each deployment held, and when).

## Rates

A **cost rate** prices one model from a moment onwards:

| Field | Meaning |
|---|---|
| `model` | The model name the gateway records (the model that answered, not the alias asked for), or `*` for every model without a rate of its own |
| `prompt_per_million`, `completion_per_million` | Price per million tokens |
| `gpu_hour` | Price per GPU-hour that model's deployments hold. Empty: GPU time is not priced (shown as such, not as 0) |
| `unit_price` | For what isn't counted in tokens: per **audio minute** for a transcription model, per **million characters** for a speech model, per **call** for a tool rate. Empty: those units are not priced |
| `effective_from` | When the rate starts. Default: now. May be in the past |
| `note` | Free text, such as the rate card it comes from |

Rates are a **schedule**, not a setting:

- **A price change is a new rate with a later start.** Rates are never edited, so a report
  for last quarter still uses last quarter's prices.
- **Each call uses the rate in force when it was made**: the latest rate for its model that
  had started by then. A model's own rate always beats the default (`*`), even an older own
  rate against a newer default.
- **A rate starting in the past re-prices reports for that period.** That is how a first
  rate card is entered. The audit entry marks it `backdated`.
- **A rate starting in the future is shown as Scheduled** and applies from its start.
- **Removing a rate** makes the period it covered fall back to the previous rate, or to
  unpriced.

Adding and removing rates is audited (`COST_RATE_CREATED`, `COST_RATE_DELETED`, with the
values and, for a removal, the reason). A rate for a model name that is neither registered
nor present in recorded usage is refused, because it could never price anything.

**Tool rates.** Agent tool calls are priced by rates named `tool:<tool name>`, with
`tool:*` as the default for tools without their own. A tool rate takes `unit_price` (the
price of one call) and nothing else, and names a registered tool or one an agent has
called. The token default `*` never prices tools, and token prices never price speech.

**Currency.** The runtime setting `chargeback.currency` (Settings page, default `USD`) is
the label every amount carries. It is a label only. Changing it converts nothing, so set it
before entering rates.

**Reports in another currency.** `CHARGEBACK__EXCHANGE_RATES` gives what one unit of
`chargeback.currency` is worth in others, for example `{"EUR": 0.92, "AED": 3.6725}`. A
report or export then takes `currency=EUR`: every amount is multiplied by that rate once,
from the full-precision total, and the report says `currency` (EUR), `priced_in` (USD)
and the `exchange_rate` used; `currencies` lists the choices. The platform never fetches
exchange rates (it may have no Internet), so they are as current as the operator keeps
them. Rates, budgets and alerts stay in `chargeback.currency`. A currency without a rate
is a 422. On the Chargeback page a currency choice appears beside the period once there
is more than one.

## Reports

`GET /chargeback/report?group_by=&start=&end=` covers whole UTC days, `start` to `end`
inclusive (default: this month so far), at most 366 days. `group_by` is one of:

| `group_by` | Rows | Notes |
|---|---|---|
| `team` | the calling key's team at the time of the call | moving a client to another team does not rewrite last month |
| `api_key` | key name, client and prefix | revoked and rotated keys keep their own row |
| `user` | platform account | calls made with a user's own token |
| `end_user` | the person behind a shared frontend | trusted and self-reported names are **separate rows**, never added together |
| `model` | the model that answered | |

Calls with no team, key, user or end user get their own row, such as "Shared (no team)".

### Token cost

Token cost is the prompt and completion tokens of each call multiplied by the rate in force
for that call. Streams the client abandoned record only the tokens seen, so they are counted
as **partial**. Their cost is a lower bound.

### Speech, OCR and tool cost

- **Speech** has no tokens. A transcription records the seconds of audio and a synthesis
  the characters spoken, and they are priced at the model's `unit_price`: per audio minute
  and per million characters. A rate without a unit price leaves speech **unpriced**; it
  is never priced at the rate's token prices (before this, the seconds and characters were
  priced as if they were tokens). Report rows show `audio_seconds`, `speech_characters`
  and `speech_cost`; speech is not added to the token columns.
- **Agent tool calls** are priced per call: every call that ran (succeeded or failed; not
  refused or still waiting for approval), at its tool's rate in force when it was asked
  for. A call belongs to the agent's team and the run's user; agent runs carry no API key
  or end user, so those groupings show tool calls on their "none" row, and the model
  grouping shows each tool as `tool:<name>`. Rows show `tool_calls` and `tool_cost`.
  Tool calls with no tool rate are counted in `unpriced_tool_calls`, apart from unpriced
  model usage, so a site that doesn't price tools isn't warned about every call.
- **Workflow tool steps** are priced the same way: a tool step that succeeded (not one
  that only asked for an approval, and not one that failed) is one call, at its tool's
  rate when the step started, on the workflow's team and the user who started the run.
- **OCR** is metered per page. Each OCR call (a scanned document or image read while
  ingesting) records a usage row with endpoint `ocr`: the pages its text came from (at
  least one) in place of tokens, the knowledge base's team, and the person who uploaded
  the document. It is priced at the OCR model's `unit_price` per page; without one it is
  unpriced. Rows show `ocr_pages` and `ocr_cost` (the console shows them in the *Speech &
  OCR* column). A failed OCR call is recorded with no pages.
- **Ingestion** is already priced: the embeddings run while ingesting documents, searching
  knowledge bases and storing memories are recorded as usage (since 0.2) and priced at the
  embedding model's token rate.

Speech, OCR and tool cost are part of `total_cost`, so cost budgets count them too.

### GPU cost

GPU cost covers platform-managed deployments only. A deployment holds its GPUs whether it
answers one call or a thousand, so:

1. **Hours.** Each model's GPU-hours in the period are the overlap of its reservations with
   the period, per GPU. A reservation is cut at every rate change, so each slice uses the
   rate in force.
2. **Sharing.** The hours are **shared out by token share**. A caller who sent 30 % of a
   model's tokens in the period carries 30 % of its GPU time and cost.
3. **Idle capacity.** Hours of a model nobody called are reported as **Idle GPU capacity**.
   They are not spread over whoever happened to call another model.
4. **External runtimes.** An Ollama, or a vLLM a team runs itself, holds no platform GPU
   reservation, so it has token cost only.

The sharing is an allocation method, not a measurement. The page and every export say so.

### Unpriced is not free

- **A call with no applicable rate** is counted, flagged `unpriced`, and contributes `null`
  cost, never 0. The report lists the models concerned (`unpriced_models`).
- **A row where nothing was priced** has a `null` total, which the page shows as
  "Not priced".
- **A rate of 0 is a price**, and shows as 0.

### Team scope

A caller without `scope.all` sees rows for their own teams and for shared usage only:

- GPU shares are still computed against **everyone's** tokens, so a team that sent 10 % of a
  model's tokens carries 10 % of its GPU time even though it cannot see the other 90 %.
- Idle capacity is shown only to callers who can see every team.

Totals cover every row the caller may see, even when the response returns only the first
1,000.

### Timings

Each row, the idle row and the totals also say how fast that usage was served, from the
same calls as its cost:

| Field | Meaning |
|---|---|
| `avg_latency_ms`, `latency_p95_ms` | mean and 95th percentile of request latency |
| `ttft_p50_ms`, `ttft_p95_ms` | median and 95th percentile time to first token |
| `output_tps_p50` | median output tokens per second |
| `timed_streams` | how many calls the TTFT and throughput figures come from |

TTFT and throughput are measured on streamed calls only (see
[gateway-policy.md](gateway-policy.md#timings)), so a row with no streamed calls has
`null` there and `timed_streams` 0. A row with no calls (idle capacity) has `null`
latency. The totals' percentiles are computed over every call the caller may see, not from
the rows: a percentile of percentiles is not a percentile. The page shows *p95 latency* and
*First token (p50)* columns, with "—" where nothing was measured.

## Export

`POST /chargeback/export` (same parameters) returns every row as CSV, including idle
capacity:

- It is a POST because it is audited (`CHARGEBACK_EXPORTED`, with the period, grouping and
  row count). The data leaves the platform.
- Each row carries the currency (the one asked for with `currency=`, if any), the period
  and `estimate=true`. `ocr_pages` and `ocr_cost` are the last two columns.
- Unpriced amounts are empty cells, not 0.
- **Timings** are the last six columns (`avg_latency_ms` to `timed_streams`), after
  `estimate`, so a spreadsheet built on the earlier columns keeps working. An unmeasured
  figure is an empty cell, not 0.
- **Formula-safe.** End-user names come from request bodies. A cell starting with `=`, `+`,
  `-`, `@`, a tab or a carriage return is prefixed with `'`, so a spreadsheet does not run
  `=HYPERLINK(...)` planted by a caller.

## Budgets

A monthly **cost budget** compares the month's estimated spend (from the 1st, 00:00 UTC)
with an amount in the `chargeback.currency` currency. A budget belongs to one scope:

| scope | subject | spend counted |
|---|---|---|
| `global` | none | everything, including idle GPU time |
| `team` | the team's id | the team's row of the report by team |
| `api_key` | the key's id | the key's row of the report by API key |

There is at most one budget per subject. Each budget has a warning share (`warn_percent`,
default 80, between 1 and 99).

- **Alerts only.** A background job (`workers.budget_check_interval_seconds`, default 300)
  raises a WARNING alert at the warning share and a CRITICAL one at 100%. Both appear on
  the Alerts page. The alert clears itself when spend falls back, for example when a new
  month starts, a rate changes or the budget is raised, turned off or removed. A budget
  never refuses traffic: these are estimates, and token **quotas** are the hard limit.
- **Unpriced usage.** If none of the subject's usage has a rate, there is no figure, so the
  budget shows *Not priced* and raises nothing. If only some usage has a rate, the figure
  is a **lower bound** and the alert says so.
- **Visibility.** Team budgets follow team scope. Global and API-key budgets need
  `scope.all`.
- **Audited.** `COST_BUDGET_CREATED`, `COST_BUDGET_UPDATED`, `COST_BUDGET_DELETED`.

The budgets card on the Chargeback page shows the amount, the spend, the share used and the
status (within budget, near limit, exceeded, not priced or off). Anyone with `cost.manage`
can add, change, turn off and remove budgets there.

Token quotas raise the same kind of alerts. See
[gateway-policy.md](gateway-policy.md#budget-alerts).

## Permissions

| Permission | Grants | Held by default |
|---|---|---|
| `cost.view` | reports, rates, budgets, CSV export | ADMIN, AI_ADMIN, AUDITOR |
| `cost.manage` | add and remove rates and budgets | ADMIN, AI_ADMIN |

They are separate from `usage.view`: a cost report is a financial view of usage, and the
rate card is a finance decision.

## Not built

- **Invoices, and budgets that refuse traffic.** Budgets only alert. Refusing
  requests on a currency figure would need rates to be authoritative, and these are
  estimates. Quotas are the hard limit, in tokens.
- **Live exchange rates.** Conversion uses the rates the operator configured; nothing
  fetches them, and a past report is converted at today's configured rate.
- **Power or utilisation-weighted GPU cost.** GPU time is reservation time, the only GPU
  measure the platform can attribute to a model reliably.
- **Tool steps that failed.** A workflow tool step that failed isn't charged, even if the
  tool ran before failing; an agent tool call that failed is.
