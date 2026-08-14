"""LLM observability: shipping agent runs to Langfuse (M19, Phase 7).

Langfuse answers the question Prometheus and Tempo cannot: *what did the model actually
say, and what did that cost*. A span shows an LLM call took 1.4 seconds; a Langfuse
trace shows the prompt, the completion, the tools it chose and the tokens it burned.

**Direct HTTP, not the `langfuse` SDK.** The SDK would add six packages to the
air-gapped bundle, two of them `requests` and `urllib3` — a second HTTP stack beside the
httpx this platform already ships. That is the same objection that kept LangGraph out in
Phase 4, and it applies harder to something that is off by default. What the SDK does
that matters here is batch, retry and back off; that is the hundred lines below, against
Langfuse's documented `/api/public/ingestion` endpoint.

**Nothing here can slow down or break a run.** Events go into a bounded in-memory queue
and a background task drains it. If Langfuse is down, the queue fills and new events are
dropped with a warning — an observability backend must never apply backpressure to the
thing it observes, and an unbounded queue in front of a dead collector is just a slower
way to run out of memory. Dropping the newest rather than evicting the oldest is
deliberate: it keeps whatever context was captured when the trouble started.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import uuid
from typing import Any

import httpx

from app.config.settings import LangfuseSettings
from app.core.logging import get_logger

log = get_logger(__name__)

#: Bounded on purpose — see the module docstring. Roughly a minute of a busy platform's
#: events at the default flush interval, which is long enough to ride out a restart of
#: the collector and short enough to be a rounding error in memory.
MAX_QUEUED_EVENTS = 2000


class LangfuseClient:
    """Batches trace events and posts them to Langfuse.

    One instance per process, built at startup when `LANGFUSE__ENABLED` is true. When it
    is false the platform builds nothing at all — see `app.main` — so the cost of the
    integration on a site that does not use it is zero, not "small".
    """

    def __init__(self, settings: LangfuseSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = client
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._task: asyncio.Task[None] | None = None
        self._dropped = 0

        secret = settings.secret_key.get_secret_value() if settings.secret_key else ""
        token = base64.b64encode(f"{settings.public_key}:{secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain_forever())

    async def stop(self) -> None:
        """Flush what is queued, then stop. Called on shutdown.

        Best-effort with a deadline: a shutdown that hangs because an observability
        backend is unreachable turns a clean restart into a `docker kill`.
        """
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                await self._flush()
        except Exception:
            log.warning("langfuse_final_flush_failed", queued=self._queue.qsize())
        finally:
            # The client owns its transport, so shutdown closes it here rather than
            # leaving an unclosed connection pool for the caller to remember.
            await self._http.aclose()

    # -- recording ---------------------------------------------------------
    def record_run(
        self,
        *,
        trace_id: str,
        agent: str,
        user_id: str | None,
        input_text: str,
        output_text: str | None,
        state: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        started_at: dt.datetime | None,
        finished_at: dt.datetime | None,
    ) -> None:
        """Queue one completed agent run as a Langfuse trace plus a generation.

        Synchronous and non-blocking by design: called from the request path, it must
        never await. `put_nowait` either succeeds or the event is dropped.

        The platform's own trace id is reused as Langfuse's, so one identifier opens the
        run in the platform, the spans in Tempo and the generation here.
        """
        trace_event = {
            "id": str(uuid.uuid4()),
            "type": "trace-create",
            "timestamp": _iso(dt.datetime.now(dt.UTC)),
            "body": {
                "id": trace_id,
                "name": f"agent:{agent}",
                "userId": user_id,
                "input": input_text,
                "output": output_text,
                "metadata": {"state": state, "agent": agent},
                "tags": [state],
            },
        }
        generation_event = {
            "id": str(uuid.uuid4()),
            "type": "generation-create",
            "timestamp": _iso(dt.datetime.now(dt.UTC)),
            "body": {
                "id": str(uuid.uuid4()),
                "traceId": trace_id,
                "name": "agent-run",
                "model": model,
                "startTime": _iso(started_at),
                "endTime": _iso(finished_at),
                "input": input_text,
                "output": output_text,
                # Langfuse's own field names. `usage` is what drives its cost and token
                # dashboards; sending our own shape would be accepted and ignored.
                "usage": {
                    "promptTokens": prompt_tokens,
                    "completionTokens": completion_tokens,
                    "totalTokens": prompt_tokens + completion_tokens,
                },
                "level": "ERROR" if state == "FAILED" else "DEFAULT",
            },
        }
        for event in (trace_event, generation_event):
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
                # Logged at a bounded rate — one line per event dropped would replace
                # the outage in the log with the reporting of it.
                if self._dropped % 100 == 1:
                    log.warning("langfuse_queue_full", dropped=self._dropped)

    # -- draining ----------------------------------------------------------
    async def _drain_forever(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._settings.flush_interval_seconds)
                await self._flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let the loop die. A crashed drain task is a queue that fills
                # silently and then drops everything for the life of the process.
                log.warning("langfuse_flush_failed", exc_info=True)

    async def _flush(self) -> None:
        batch: list[dict[str, Any]] = []
        while len(batch) < self._settings.flush_batch_size and not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if not batch:
            return

        url = f"{self._settings.host.rstrip('/')}/api/public/ingestion"
        response = await self._http.post(
            url,
            json={"batch": batch},
            headers=self._headers,
            timeout=self._settings.timeout_seconds,
        )
        if response.status_code >= 400:
            # Not requeued. A rejected batch is nearly always malformed or
            # unauthenticated, and retrying it forever would block every later event
            # behind an event that can never succeed.
            log.warning(
                "langfuse_rejected_batch",
                status=response.status_code,
                events=len(batch),
                detail=response.text[:200],
            )


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
