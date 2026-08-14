"""GPU placement (§9, §23).

V1 is deliberately simple, as §9 asks: filter nodes that can fit the model, prefer the
least loaded, reserve atomically. §9's "later add" list — topology, NVLink, NUMA, memory
fragmentation, priority, tenant quotas — becomes a second `Scheduler` implementation
rather than an edit to this one, because placement is the part of this system most
likely to accumulate policy.

The interesting part is not the choosing, it is the **reserving**. Selection reads a
snapshot of availability, and any two concurrent requests can read the same snapshot.
Exclusion therefore cannot live here; it lives in the partial unique index on
`gpu_allocations` (Phase 1), and `reserve()` simply lets the database say no.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.interfaces.scheduler import (
    GpuResource,
    Placement,
    PlacementFailure,
    PlacementRequest,
    Scheduler,
)
from app.core.logging import get_logger
from app.services.infrastructure import GpuService

log = get_logger(__name__)


class SimpleGpuScheduler(Scheduler):
    """First-fit least-loaded placement."""

    def __init__(self, gpus: GpuService) -> None:
        self._gpus = gpus

    async def plan(
        self, request: PlacementRequest, available: Sequence[GpuResource]
    ) -> Placement | PlacementFailure:
        """Choose GPUs. Pure — no side effects, so it is trivially testable."""
        if request.gpu_indices:
            return self._plan_explicit(request, available)

        by_node: dict[str, list[GpuResource]] = {}
        for gpu in available:
            if gpu.reserved:
                continue
            if gpu.memory_free_mib < request.required_memory_mib_per_gpu:
                continue
            by_node.setdefault(gpu.node_id, []).append(gpu)

        if request.node_id is not None:
            by_node = {k: v for k, v in by_node.items() if k == request.node_id}

        candidates = {
            node_id: gpus for node_id, gpus in by_node.items() if len(gpus) >= request.gpu_count
        }
        if not candidates:
            return self._explain_failure(request, available)

        # Least *total* utilisation across the node's free GPUs. Spreading load across
        # nodes beats packing: a second deployment landing on an already-busy host
        # contends for PCIe and host memory even when its own GPUs are free.
        node_id = min(
            candidates,
            key=lambda n: sum(g.utilization_percent for g in candidates[n]) / len(candidates[n]),
        )
        chosen = sorted(candidates[node_id], key=lambda g: (g.utilization_percent, g.index))
        indices = tuple(sorted(g.index for g in chosen[: request.gpu_count]))

        return Placement(
            node_id=node_id,
            gpu_indices=indices,
            reservation_id="",  # assigned by reserve()
            rationale=(
                f"least-loaded node with {request.gpu_count} free GPU(s) of "
                f">={request.required_memory_mib_per_gpu} MiB"
            ),
        )

    def _plan_explicit(
        self, request: PlacementRequest, available: Sequence[GpuResource]
    ) -> Placement | PlacementFailure:
        """Honour an operator's explicit device choice, but still validate it.

        Accepting indices blindly would let a request pin a deployment onto GPUs that
        are already held, and the failure would surface as a CUDA OOM inside the
        container rather than as a scheduling error here.
        """
        assert request.gpu_indices is not None  # noqa: S101 — guarded by the caller
        if request.node_id is None:
            return PlacementFailure(reason="Explicit GPU indices require a node_id.")

        on_node = {g.index: g for g in available if g.node_id == request.node_id}
        missing = [i for i in request.gpu_indices if i not in on_node]
        if missing:
            return PlacementFailure(
                reason=f"Node has no GPU with index {missing}.",
                considered_nodes=(request.node_id,),
                details={"available": ",".join(str(i) for i in sorted(on_node))},
            )

        held = [i for i in request.gpu_indices if on_node[i].reserved]
        if held:
            return PlacementFailure(
                reason=f"GPU(s) {held} are already reserved on this node.",
                considered_nodes=(request.node_id,),
            )

        short = [
            i
            for i in request.gpu_indices
            if on_node[i].memory_free_mib < request.required_memory_mib_per_gpu
        ]
        if short:
            return PlacementFailure(
                reason=(
                    f"GPU(s) {short} have less than {request.required_memory_mib_per_gpu} MiB free."
                ),
                considered_nodes=(request.node_id,),
            )

        return Placement(
            node_id=request.node_id,
            gpu_indices=tuple(sorted(request.gpu_indices)),
            reservation_id="",
            rationale="explicit GPU selection",
        )

    @staticmethod
    def _explain_failure(
        request: PlacementRequest, available: Sequence[GpuResource]
    ) -> PlacementFailure:
        """Say *why* nothing fit, per node.

        "No node has 2 GPUs with 80 GiB free; node-01 has 1 free, node-02 is at 95%
        utilisation" is something an operator can act on. "Scheduling failed" sends them
        to read the source.
        """
        per_node: dict[str, str] = {}
        for gpu in available:
            free = [
                g
                for g in available
                if g.node_id == gpu.node_id
                and not g.reserved
                and g.memory_free_mib >= request.required_memory_mib_per_gpu
            ]
            total = sum(1 for g in available if g.node_id == gpu.node_id)
            reserved = sum(1 for g in available if g.node_id == gpu.node_id and g.reserved)
            per_node[gpu.node_id] = (
                f"{len(free)} of {total} GPUs usable ({reserved} reserved, "
                f"needs >={request.required_memory_mib_per_gpu} MiB free)"
            )

        if not available:
            reason = (
                "No GPUs are known to the platform. Register a GPU node before deploying a model."
            )
        else:
            reason = (
                f"No node has {request.gpu_count} free GPU(s) with at least "
                f"{request.required_memory_mib_per_gpu} MiB each."
            )
        return PlacementFailure(
            reason=reason, considered_nodes=tuple(sorted(per_node)), details=per_node
        )

    async def reserve(self, request: PlacementRequest, placement: Placement) -> Placement:
        """Claim the chosen GPUs.

        Delegates to `GpuService.reserve`, which inserts inside a SAVEPOINT so a losing
        race raises a clean 409 and leaves the caller's transaction usable. Must run in
        the same transaction as the deployment row it belongs to.
        """
        reservation_id = await self._gpus.reserve(
            node_id=uuid.UUID(placement.node_id),
            gpu_indices=list(placement.gpu_indices),
            purpose=f"model:{request.model_id}",
        )
        return Placement(
            node_id=placement.node_id,
            gpu_indices=placement.gpu_indices,
            reservation_id=str(reservation_id),
            rationale=placement.rationale,
        )

    async def release(self, reservation_id: str) -> None:
        """Idempotent — deployment cleanup paths retry."""
        await self._gpus.release(uuid.UUID(reservation_id))


async def gpu_resources_for(
    gpu_service: GpuService, node_ids: Sequence[uuid.UUID]
) -> list[GpuResource]:
    """Build the scheduler's view of current availability.

    Combines inventory, the latest telemetry sample and active reservations. Free memory
    comes from *observed* usage rather than from what the platform believes it scheduled:
    a process the platform did not start still consumes the memory, and placing on top of
    it would OOM.
    """
    resources: list[GpuResource] = []
    for node_id in node_ids:
        gpus = await gpu_service.list_gpus(node_id)
        if not gpus:
            continue
        latest = await gpu_service.latest_metrics(gpus)
        free = set(await gpu_service.free_indices(node_id))
        for gpu in gpus:
            metric = latest.get(gpu.id)
            resources.append(
                GpuResource(
                    node_id=str(node_id),
                    index=gpu.index,
                    uuid=gpu.uuid,
                    memory_total_mib=gpu.memory_total_mib,
                    memory_used_mib=metric.memory_used_mib if metric else 0,
                    utilization_percent=metric.utilization_percent if metric else 0.0,
                    reserved=gpu.index not in free,
                )
            )
    return resources
