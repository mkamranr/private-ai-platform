# Observability (M19)

Four questions, four tools, one identifier tying them together.

| question | tool | where it comes from |
|---|---|---|
| Is it healthy, and how fast? | Prometheus | `GET /metrics`, scraped every 15s |
| What did it say when it broke? | Loki | container logs, shipped by Alloy |
| Where did the time go? | Tempo | OTLP spans from the control plane |
| What did the model actually do? | Langfuse | agent runs, pushed by the platform |
| …and without any of the above? | the platform | `/monitoring/overview`, `/traces/{id}` |

That last row is the important one. **The platform is fully observable with none of these
deployed**: `/metrics` is always exposed, every agent run is stamped with a trace id and
its §11 event sequence is in Postgres, and `/traces/{trace_id}` reads it back. The
collectors add history, search and graphs — they are not where the data lives.

## Starting it

```bash
make monitoring     # prometheus, loki, tempo, grafana, alloy, langfuse
```

```
Grafana    http://localhost:8080/grafana/     admin / GRAFANA__ADMIN_PASSWORD
Langfuse   http://localhost:8084/          (production: https://langfuse.<your-host>)
```

Grafana is served under a path on the platform's single published port, which it
supports properly. Langfuse is routed **by name** instead, exactly as Open WebUI is and
for the same reason: it is a Next.js application whose asset paths are fixed at build
time, so under a path it returns a working API and a blank page. The published port is
a developer convenience (8084 by default, `DEV_LANGFUSE_PORT` to change it); production
publishes only port 80 and routes by name.

Nothing else in the stack is exposed: Prometheus, Loki and Tempo are reachable only
from inside the `ai-platform` network.

The **collectors** and the **exporters** are separate switches, and both default to off:

```bash
TRACING__ENABLED=true      # the backend exports spans to Tempo
LANGFUSE__ENABLED=true     # the backend pushes agent runs to Langfuse
make restart-backend
```

An exporter pointed at a collector that was never deployed does not fail quietly — it
queues, retries and logs an export failure every few seconds for the life of the
process. So neither turns itself on because the profile happens to be running.

## The identifier that joins them up

Every request carries a **request id**; when tracing is on it also carries a **trace id**,
and both are on every log line. They are not redundant:

- the **request id** exists always, and is what error responses and audit records carry;
- the **trace id** exists when tracing is deployed, and is what Tempo and Langfuse index.

An operator starting from an error a user actually saw has the request id. Grepping it
in Loki yields the line, the line carries the trace id, and the trace id opens the span
in Tempo, the generation in Langfuse and the run in `/traces/{trace_id}`. Carrying only
one of the two would break that chain at its first step.

Agent runs get a trace id **whether or not tracing is deployed** — the real OTel id when
it is, a minted one when it is not — so `/traces/{trace_id}` answers on every install.

## What is measured

Metric names are prefixed `ai_platform_`. The full list is in
[`backend/app/core/metrics.py`](../backend/app/core/metrics.py); the shape matters more
than the list:

- **HTTP** — requests and duration, by method, route **template** and status *class*
- **Gateway** — requests, tokens by model, and time-to-first-token
- **Agents** — runs by terminal state, run duration, tool calls by outcome
- **Fleet** — nodes, GPUs, deployments and per-GPU utilisation, read at scrape time

Three rules hold everywhere, and each exists because breaking it is silent:

**Labels are bounded.** `/api/v1/models/{model_id}` is a label; `/api/v1/models/8f3e…`
is a bug. Prometheus keeps one time series per label combination for ever, so an id in a
label is an unbounded memory leak that never raises an error. The label also keeps its
prefix, because the gateway's `/v1/models` and the registry's `/api/v1/models` are two
different resources behind two different credentials.

**Status is a class, not a code.** Nobody pages on the difference between 404 and 409.
The exact code is in the access log and the audit record.

**Fleet state is read, not pushed.** A gauge incremented from event handlers is
permanently wrong after one missed event; a query cannot drift.

## Retention

| | | why |
|---|---|---|
| Prometheus | 30 days | beyond that, `usage_records` answers capacity questions better — and it is backed up |
| Loki | 30 days | long enough for an incident reported late, short enough for a local disk |
| Tempo | 14 days | spans are large per unit of insight; the platform's own run record does not expire |

None of it is in the M25 backup, deliberately. Telemetry is reproducible and enormous;
the system of record is Postgres, MinIO and Qdrant. Restoring a platform does not
restore its graphs, and should not try to.

## Alerts

`docker/prometheus/rules/platform.yml` — five rules, deliberately few. A rule that fires
often enough to be ignored is worse than no rule.

There is **no Alertmanager**. An air-gapped site has no SMTP relay, no PagerDuty and no
Slack; alerts surface in Grafana's alert list. A site with an internal notification
channel can add one and point these rules at it.

## The Docker socket

Alloy reads it, read-only. That is the platform's one exception to "only the node agent
holds the socket", and the reasoning is in
[`docker/alloy/config.alloy`](../docker/alloy/config.alloy): routing every container's
log stream through the node agent would put the control path inside the observability
path, so an incident that breaks the agent would also blind the operator investigating
it.

## Langfuse

The **v2 line**, on the platform's own Postgres in a separate `langfuse` database. v3
adds ClickHouse plus a worker, which roughly triples the air-gap bundle and puts a
second database in front of an operator who has to back it up offline.

The platform talks to its ingestion API over httpx rather than through the `langfuse`
SDK. The SDK would add six packages including `requests` and `urllib3` — a second HTTP
stack beside the one already shipped — which is the same objection that kept LangGraph
out in Phase 4. See [`app/services/langfuse.py`](../backend/app/services/langfuse.py).

Events are queued in memory and flushed by a background task. If Langfuse is down the
queue fills and new events are dropped with a warning: an observability backend must
never apply backpressure to the thing it observes.

Get the keys from Langfuse itself (create a project → Settings → API keys), put them in
`.env` as `LANGFUSE__PUBLIC_KEY` / `LANGFUSE__SECRET_KEY`, and restart the backend.

## Air-gapped sites

The monitoring images are **opt-in** in the bundle, because they add ~2.5 GB to media
that is physically carried:

```bash
make bundle MONITORING=1
```

Without the flag the bundle installs a platform whose `/metrics`, `/monitoring/overview`
and `/traces/{id}` all work, and which simply has nowhere to send spans.

Every collector is configured with its telemetry reporting off explicitly, rather than
relying on the network being unreachable — so the intent survives someone testing on a
connected machine (Rule 4).

## The gate

```bash
make gate-phase7
```

It does not check that containers are running. It asserts data **arrives**: that
Prometheus holds a platform metric it scraped, that Loki holds a line the backend wrote,
that Tempo returns the trace named on that line, and that Grafana can reach every
datasource it was provisioned with.

Two of its checks exist because of bugs found while building this phase, both of which
look fine from `curl`:

- Declaring the OpenMetrics content type while writing the Prometheus text format makes
  every scrape fail with `data does not end with # EOF`. The endpoint returns 200 with a
  body full of metrics; the target just reports `up 0` for ever.
- A shell variable does not override `env_file`, so `TRACING__ENABLED=true` in front of a
  compose command silently did nothing — and an assertion looking for `"enabled":true`
  anywhere in the overview matched the always-on metrics collector and passed.
