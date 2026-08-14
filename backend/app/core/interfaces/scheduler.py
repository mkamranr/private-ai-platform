"""Scheduler — GPU placement decisions (§9, §23).

V1 (Phase 2) is deliberately simple: filter nodes that can fit the model, pick the
least loaded, reserve atomically. §9's "later add" list — topology, NVLink, NUMA,
memory fragmentation, priority, tenant quotas — becomes a second implementation
rather than an edit, because placement is the part of this system most likely to
grow policy.

The reservation contract is the important part. Two concurrent deploy requests can
otherwise both observe GPUs 0 and 1 as free and both claim them; the first vLLM
container wins and the second dies with a CUDA OOM that looks like a model
problem. The spec has no allocation table, so this interface requires one:
:meth:`Scheduler.reserve` must persist the claim in the same transaction that
creates the deployment, guarded by a unique index on
``(node_id, gpu_index) WHERE released_at IS NULL``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GpuResource:
    """A GPU as the scheduler sees it."""

    node_id: str
    index: int
    uuid: str
    memory_total_mib: int
    memory_used_mib: int
    utilization_percent: float
    # True when another deployment already holds this device.
    reserved: bool = False

    @property
    def memory_free_mib(self) -> int:
        return max(0, self.memory_total_mib - self.memory_used_mib)


@dataclass(frozen=True, slots=True)
class PlacementRequest:
    model_id: str
    gpu_count: int
    required_memory_mib_per_gpu: int
    # Pin to a node, or None to let the scheduler choose.
    node_id: str | None = None
    # Explicit device indices, bypassing selection but still reserved.
    gpu_indices: tuple[int, ...] | None = None
    # Tensor parallelism generally wants devices on one node sharing NVLink.
    require_same_node: bool = True


@dataclass(frozen=True, slots=True)
class Placement:
    node_id: str
    gpu_indices: tuple[int, ...]
    reservation_id: str
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class PlacementFailure:
    """Why placement failed, in terms an operator can act on.

    "No node has 2 GPUs with 80 GiB free; node-01 has 1, node-02 is at 95%
    utilisation" is actionable. "Scheduling failed" is not.
    """

    reason: str
    considered_nodes: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)


class Scheduler(ABC):
    """Chooses and reserves GPUs for model deployments."""

    @abstractmethod
    async def plan(
        self,
        request: PlacementRequest,
        available: Sequence[GpuResource],
    ) -> Placement | PlacementFailure:
        """Pure placement decision. No side effects, so it is trivially testable."""
        ...

    @abstractmethod
    async def reserve(self, request: PlacementRequest, placement: Placement) -> Placement:
        """Persist the claim. Must be atomic and must fail on a conflicting claim."""
        ...

    @abstractmethod
    async def release(self, reservation_id: str) -> None:
        """Free a reservation. Must be idempotent — cleanup paths retry."""
        ...
