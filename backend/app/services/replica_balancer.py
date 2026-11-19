"""Spreading a model's traffic across its running copies (E58).

A model can have several running deployments: copies on different GPUs or nodes. Before
E58 the gateway always picked the first, so extra copies added failover but no capacity.
Now each call goes to the copy with the fewest calls in flight, and copies that are equally
busy take turns.

**In-flight counts live in Valkey**, as a sorted set of leases per deployment, so every
control-plane replica sees the same numbers. The same lease pattern as gateway concurrency
quotas: a control-plane process that dies mid-call cannot release its lease, so each one
lapses after ``gateway.concurrency_lease_seconds`` instead of marking that copy busy
forever.

**Turn-taking** is an ``INCR`` on a per-model counter, also in Valkey. Without it, equally
busy copies (the normal state of a quiet system) would all sort the same way and the
first would take every call.

**Valkey being down never stops traffic.** Ordering falls back to a counter in this
process, which still rotates, and leases are skipped. Load balancing gets worse, calls keep
working; the same "fail open" rule as rate limits.

Strategies (``gateway.load_balancing``):

* ``least_busy`` (default): fewest calls in flight, turn-taking between equals. Right for
  LLM serving, where one long generation can occupy a copy for a minute while another
  finishes a dozen short ones.
* ``round_robin``: strict turns, ignoring load.
* ``first``: always the oldest copy, the others only on failure. The behaviour before
  E58, kept for debugging (repeated calls hit the same instance).
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging import get_logger

log = get_logger(__name__)

STRATEGIES = ("least_busy", "round_robin", "first")

#: Rotation when Valkey is unreachable. Process-wide: each control-plane replica rotates on
#: its own, which still spreads load, just less evenly.
_LOCAL_TURNS = itertools.count()


class _HasId(Protocol):
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ReplicaLease:
    """One call in flight on one deployment."""

    key: str
    member: str


def _inflight_key(deployment_id: uuid.UUID) -> str:
    return f"gw:inflight:{deployment_id}"


def _turn_key(model_id: uuid.UUID) -> str:
    return f"gw:turn:{model_id}"


class ReplicaBalancer:
    def __init__(self, redis: Any, lease_seconds: int, strategy: str = "least_busy") -> None:
        self._redis = redis
        self._lease = lease_seconds
        self._strategy = strategy if strategy in STRATEGIES else "least_busy"

    async def order[T: _HasId](self, model_id: uuid.UUID, deployments: Sequence[T]) -> list[T]:
        """``deployments`` in the order to try them: the first gets the call, the rest are
        where a failed call is retried."""
        candidates = sorted(deployments, key=lambda d: str(d.id))
        if len(candidates) <= 1 or self._strategy == "first":
            return list(deployments)
        turn = await self._turn(model_id)
        offset = turn % len(candidates)
        rotated = candidates[offset:] + candidates[:offset]
        if self._strategy == "round_robin":
            return rotated
        busy = await self._in_flight([d.id for d in rotated])
        if busy is None:
            return rotated
        # Stable sort: equally busy copies keep their rotated order, so they take turns.
        return sorted(rotated, key=lambda d: busy.get(d.id, 0))

    async def acquire(self, deployment_id: uuid.UUID) -> ReplicaLease | None:
        """Count a call against a deployment until ``release``. None when Valkey is down:
        the call still runs, it just is not counted."""
        key = _inflight_key(deployment_id)
        member = uuid.uuid4().hex
        now = time.time()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", now - self._lease)
                pipe.zadd(key, {member: now})
                pipe.expire(key, self._lease)
                await pipe.execute()
        except Exception as exc:
            log.debug("replica_lease_unavailable", error=type(exc).__name__)
            return None
        return ReplicaLease(key, member)

    async def release(self, lease: ReplicaLease | None) -> None:
        if lease is None:
            return
        try:
            await self._redis.zrem(lease.key, lease.member)
        except Exception as exc:
            # The lease lapses on its own; logged so a persistent failure is visible.
            log.warning("replica_lease_release_failed", error=type(exc).__name__)

    async def _turn(self, model_id: uuid.UUID) -> int:
        try:
            return int(await self._redis.incr(_turn_key(model_id)))
        except Exception:
            return next(_LOCAL_TURNS)

    async def _in_flight(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int] | None:
        cutoff = time.time() - self._lease
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for ident in ids:
                    pipe.zcount(_inflight_key(ident), cutoff, "+inf")
                counts = await pipe.execute()
        except Exception as exc:
            log.debug("replica_counts_unavailable", error=type(exc).__name__)
            return None
        return {ident: int(count or 0) for ident, count in zip(ids, counts, strict=True)}
