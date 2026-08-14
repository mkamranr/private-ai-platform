"""Reconcile must not report a clean platform after a scan that could not look (M08).

The bug this guards: a node whose agent times out is skipped, the orphan list comes back
empty, and every caller renders that as "nothing to clean up". An operator reads it as a
clean bill of health and walks away — while orphaned containers keep holding GPUs on the
one node nobody could see. An unreachable node is precisely where orphans accumulate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.infrastructure import NodeStatus
from app.services.deployment import DeploymentService


class _Nodes:
    def __init__(self, nodes) -> None:
        self._nodes = nodes

    async def list_all(self, **_kwargs):
        return self._nodes


class _Deployments:
    async def list_deployments(self, **_kwargs):
        return []


@pytest.fixture
def service() -> DeploymentService:
    """A service with only the collaborators reconcile touches.

    Constructed without __init__ deliberately: the real one needs six repositories and a
    GPU service, none of which this behaviour depends on, and wiring them would test the
    constructor rather than the scan.
    """
    instance = DeploymentService.__new__(DeploymentService)
    instance.last_unscanned_nodes = []
    return instance


class TestReconcileCoverage:
    async def test_an_offline_node_is_reported_as_unscanned(self, service) -> None:
        service._deployments = _Deployments()
        service._nodes = _Nodes([SimpleNamespace(name="gpu-01", status=NodeStatus.OFFLINE)])

        orphans = await service.reconcile_orphans(remove=False)

        # No orphans found — but only because nothing could be looked at.
        assert orphans == []
        assert service.last_unscanned_nodes == ["gpu-01 (OFFLINE)"]

    async def test_an_unreachable_agent_is_reported_as_unscanned(self, service) -> None:
        """The exact failure seen in practice: the node is ONLINE in the database, and
        its agent then times out mid-scan."""
        service._deployments = _Deployments()
        service._nodes = _Nodes([SimpleNamespace(name="gpu-02", status=NodeStatus.ONLINE)])

        def _explode(_node):
            raise TimeoutError("node agent did not respond within 15.0s")

        service._backend = _explode

        orphans = await service.reconcile_orphans(remove=False)

        assert orphans == []
        assert service.last_unscanned_nodes == ["gpu-02 (unreachable)"]

    async def test_a_complete_scan_reports_full_coverage(self, service) -> None:
        """The clean case must stay clean: nothing unscanned, so callers may say so."""
        service._deployments = _Deployments()
        service._nodes = _Nodes([SimpleNamespace(name="gpu-03", status=NodeStatus.ONLINE)])

        class _Backend:
            async def list_managed(self):
                return []

        service._backend = lambda _node: _Backend()

        orphans = await service.reconcile_orphans(remove=False)

        assert orphans == []
        assert service.last_unscanned_nodes == []
