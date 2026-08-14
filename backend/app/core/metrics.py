"""Prometheus metrics (M19, Phase 7).

One registry, defined in one place, because the failure mode of ad-hoc metrics is not a
crash — it is a dashboard that has quietly measured the wrong thing for a month.

Three rules, each of which exists because breaking it is expensive:

**Labels are bounded.** Every label value here comes from a fixed set: an HTTP method, a
*route template*, a status class, a deployment state. Never a user id, a model id, a
path with an id in it, or anything else unbounded — Prometheus keeps one time series per
label combination, and an unbounded label is how a monitoring stack runs the host out of
memory. `/models/{model_id}` is the label; `/models/8f3e…` is a bug.

**Business counters are incremented where the thing happens**, not derived at scrape
time. A token count reconstructed from the database at scrape time is a query on every
scrape and a number that changes when history is pruned.

**Fleet state is read at scrape time**, not pushed. Node and GPU counts are gauges of
*current* state; incrementing them from event handlers means a missed event leaves the
gauge permanently wrong, whereas a query cannot drift. See :func:`collect_fleet_state`.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

#: The platform's own registry, never the library's global one. The default registry is
#: process-wide and picks up whatever any imported dependency decides to register, which
#: makes the exposition output depend on import order.
REGISTRY = CollectorRegistry()

# ---------------------------------------------------------------------------
# HTTP (recorded by app.core.middleware)
# ---------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "ai_platform_http_requests_total",
    "HTTP requests handled, by route template and status class.",
    ("method", "route", "status"),
    registry=REGISTRY,
)

HTTP_DURATION = Histogram(
    "ai_platform_http_request_duration_seconds",
    "Wall-clock time to produce a response.",
    ("method", "route"),
    # Tuned for this API rather than left at the library default. The default's top
    # finite bucket is 10s, which puts a model deployment and a slow document ingest in
    # the same bucket as +Inf and makes both unmeasurable.
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Gateway (M09) — recorded by app.services.gateway
# ---------------------------------------------------------------------------
GATEWAY_REQUESTS = Counter(
    "ai_platform_gateway_requests_total",
    "Inference requests through the OpenAI-compatible surface.",
    ("model", "surface", "outcome"),
    registry=REGISTRY,
)

GATEWAY_TOKENS = Counter(
    "ai_platform_gateway_tokens_total",
    "Tokens accounted through the gateway.",
    # `kind` is prompt|completion. Deliberately not per-user: attribution lives in
    # usage_records, which is queryable and prunable. A per-user label would add one
    # time series per person per model, permanently.
    ("model", "kind"),
    registry=REGISTRY,
)

GATEWAY_TTFT = Histogram(
    "ai_platform_gateway_time_to_first_token_seconds",
    "Time from request to first streamed token — what a chat user actually feels.",
    ("model",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Agents (M10-M12) - recorded by the runtime
# ---------------------------------------------------------------------------
AGENT_RUNS = Counter(
    "ai_platform_agent_runs_total",
    "Agent runs by terminal state.",
    ("agent", "state"),
    registry=REGISTRY,
)

AGENT_RUN_DURATION = Histogram(
    "ai_platform_agent_run_duration_seconds",
    "Agent run wall-clock duration, from start to terminal state.",
    ("agent",),
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0),
    registry=REGISTRY,
)

TOOL_CALLS = Counter(
    "ai_platform_tool_calls_total",
    "Tool invocations by outcome, including the ones authorisation refused.",
    ("tool", "outcome"),
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Fleet state (collected at scrape time — see collect_fleet_state)
# ---------------------------------------------------------------------------
NODES = Gauge(
    "ai_platform_nodes",
    "Registered nodes by status.",
    ("status",),
    registry=REGISTRY,
)

GPUS = Gauge(
    "ai_platform_gpus",
    "Registered GPUs by health.",
    ("health",),
    registry=REGISTRY,
)

GPU_UTILISATION = Gauge(
    "ai_platform_gpu_utilisation_percent",
    "Most recent utilisation sample per GPU.",
    ("node", "gpu_index"),
    registry=REGISTRY,
)

GPU_MEMORY_USED = Gauge(
    "ai_platform_gpu_memory_used_bytes",
    "Most recent memory-used sample per GPU.",
    ("node", "gpu_index"),
    registry=REGISTRY,
)

DEPLOYMENTS = Gauge(
    "ai_platform_deployments",
    "Model deployments by state (M08).",
    ("state",),
    registry=REGISTRY,
)

BUILD_INFO = Gauge(
    "ai_platform_build_info",
    "Always 1. The labels carry the version — the standard way to expose build metadata.",
    ("version", "environment"),
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    """The exposition payload and its content type.

    Both from the same module, and that is the whole point. Pairing `generate_latest`
    with the *OpenMetrics* content type produces a body Prometheus refuses with
    ``data does not end with # EOF``: OpenMetrics requires a terminating marker the
    text format does not write. The endpoint still returns 200 with a body full of
    metrics, so it looks correct from `curl` and from any test that checks for a metric
    name — the target simply reports `up 0` and every dashboard stays empty.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def status_class(status_code: int) -> str:
    """`2xx`, `4xx`, … — the label, rather than the code itself.

    One series per status *code* per route is a lot of series for information nobody
    alerts on; nobody pages on the difference between 404 and 409, they page on the 4xx
    rate. The exact code is in the access log and the audit record.
    """
    return f"{status_code // 100}xx"
