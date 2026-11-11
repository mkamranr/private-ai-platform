"""Workflow worker (E6): fires schedules and advances runs that are due.

A plain asyncio loop like the ingestion worker, not an APScheduler job: a run can take
minutes (a model call, a slow HTTP service), and a fixed-interval job that overruns fights
its own next firing. Each tick does one unit of work in its own transaction:

1. queue one scheduled run whose time has come, or
2. advance one run that is queued or whose delay has elapsed.

Claims use ``FOR UPDATE SKIP LOCKED``, so a second replica would take different rows
rather than the same ones. Runs paused for approval are not touched here: the decision
continues them, inside the request that records it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.services.workflows import WorkflowService

log = get_logger(__name__)

IDLE_POLL_SECONDS: Final = 2.0
ERROR_BACKOFF_SECONDS: Final = 15.0

ServiceFactory = Callable[[AsyncSession], WorkflowService]


class WorkflowWorker:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], service_factory: ServiceFactory
    ) -> None:
        self._session_factory = session_factory
        # Injected from the composition root: the workflow stack needs the gateway,
        # knowledge and tool executors, assembled the same way a request assembles them.
        self._service_factory = service_factory

    async def tick(self) -> bool:
        """One unit of work. Returns whether there was any."""
        now = dt.datetime.now(dt.UTC)
        async with self._session_factory() as session:
            service = self._service_factory(session)
            try:
                run = await service.fire_due_schedule(now)
                if run is not None:
                    await session.commit()
                    log.info("workflow_schedule_fired", run=str(run.id))
                    return True
                run = await service.claim_due_run(now)
                if run is None:
                    # Commit, not roll back: a schedule whose run could not start has still
                    # been moved on, and rolling that back would refire it every tick.
                    await session.commit()
                    return False
                await session.commit()
                log.info("workflow_run_advanced", run=str(run.id), status=run.status)
                return True
            except Exception:
                await session.rollback()
                raise

    async def run_forever(self) -> None:
        log.info("workflow_worker_started", idle_poll_seconds=IDLE_POLL_SECONDS)
        try:
            while True:
                try:
                    had_work = await self.tick()
                except Exception:
                    log.exception("workflow_worker_tick_failed")
                    await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                    continue
                if not had_work:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("workflow_worker_stopped")
            raise
