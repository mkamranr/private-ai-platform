"""Model pulls (E97): fetches models onto backends (Ollama) one at a time.

A plain asyncio loop like the answer-scoring worker: a pull can run for many minutes.
The claim is its own transaction (``FOR UPDATE SKIP LOCKED``, so replicas take different
pulls), then progress is committed every couple of seconds while Ollama streams it. A
pull whose worker stopped (no heartbeat for two minutes) is claimed again, and Ollama
resumes what it had downloaded. Finished pulls older than a week are pruned hourly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.services.inference_backends import InferenceBackendService

log = get_logger(__name__)

IDLE_POLL_SECONDS: Final = 5.0
ERROR_BACKOFF_SECONDS: Final = 30.0
PRUNE_EVERY_SECONDS: Final = 3600.0

ServiceFactory = Callable[[AsyncSession], InferenceBackendService]


class BackendPullWorker:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], service_factory: ServiceFactory
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._pruned_at = 0.0

    async def tick(self) -> bool:
        """One pull run to its end. Returns whether there was one."""
        async with self._session_factory() as session:
            service = self._service_factory(session)
            try:
                if time.monotonic() - self._pruned_at > PRUNE_EVERY_SECONDS:
                    removed = await service.prune_pulls()
                    self._pruned_at = time.monotonic()
                    if removed:
                        log.info("backend_pulls_pruned", removed=removed)
                pull = await service.claim_pull()
                await session.commit()
                if pull is None:
                    return False
                log.info("backend_pull_started", pull=str(pull.id), model=pull.model)
                await service.run_pull(pull, session.commit)
            except Exception:
                await session.rollback()
                raise
        return True

    async def run_forever(self) -> None:
        log.info("backend_pull_worker_started", idle_poll_seconds=IDLE_POLL_SECONDS)
        try:
            while True:
                try:
                    had_work = await self.tick()
                except Exception:
                    log.exception("backend_pull_tick_failed")
                    await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                    continue
                if not had_work:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("backend_pull_worker_stopped")
            raise
