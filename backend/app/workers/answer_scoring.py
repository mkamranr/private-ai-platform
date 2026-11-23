"""Answer-scoring worker (E70): judges sampled agent answers, one at a time.

A plain asyncio loop like the workflow worker: a judge call can take tens of seconds, and
an interval job that overran would fight its own next firing. Each tick judges one queued
answer in its own transaction; claims use ``FOR UPDATE SKIP LOCKED``, so replicas take
different answers. Old scores are pruned hourly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.services.answer_scoring import AnswerScoringService

log = get_logger(__name__)

IDLE_POLL_SECONDS: Final = 10.0
ERROR_BACKOFF_SECONDS: Final = 30.0
PRUNE_EVERY_SECONDS: Final = 3600.0

ServiceFactory = Callable[[AsyncSession], AnswerScoringService]


class AnswerScoringWorker:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], service_factory: ServiceFactory
    ) -> None:
        self._session_factory = session_factory
        self._service_factory = service_factory
        self._pruned_at = 0.0

    async def tick(self) -> bool:
        """One answer judged (and, hourly, old scores pruned). Returns whether there was
        an answer to judge."""
        async with self._session_factory() as session:
            service = self._service_factory(session)
            try:
                if time.monotonic() - self._pruned_at > PRUNE_EVERY_SECONDS:
                    removed = await service.prune()
                    self._pruned_at = time.monotonic()
                    if removed:
                        log.info("answer_scores_pruned", removed=removed)
                row = await service.score_next()
                await session.commit()
            except Exception:
                await session.rollback()
                raise
        if row is not None:
            log.info(
                "agent_answer_scored", score=str(row.id), status=row.status, value=row.faithfulness
            )
        return row is not None

    async def run_forever(self) -> None:
        log.info("answer_scoring_worker_started", idle_poll_seconds=IDLE_POLL_SECONDS)
        try:
            while True:
                try:
                    had_work = await self.tick()
                except Exception:
                    log.exception("answer_scoring_tick_failed")
                    await asyncio.sleep(ERROR_BACKOFF_SECONDS)
                    continue
                if not had_work:
                    await asyncio.sleep(IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            log.info("answer_scoring_worker_stopped")
            raise
