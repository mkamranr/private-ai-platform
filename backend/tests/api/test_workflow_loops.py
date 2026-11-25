"""Edges back (E84): a branch that leads back to an earlier step runs that stretch of the
workflow again, at most ``max_loops`` times, with the last pass's outputs as ``previous``.

Uses the real engine and service on the test database, with the fakes from
``test_workflows.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.models.workflows import WorkflowStep
from tests.api import test_workflows as base
from tests.api.test_workflow_parallel import SlowProvider
from tests.api.test_workflows import node, published, run

pytestmark = pytest.mark.asyncio

wf = base.wf
builder = base.builder


def redraft(needle: str, max_loops: int = 3) -> dict[str, Any]:
    """Draft, check the draft for ``needle``, and draft again (seeing the last one) if not."""
    return {
        "nodes": [
            node("start", "start"),
            node("draft", "llm", model="m", prompt="draft {{ previous.draft.text }}"),
            node(
                "check",
                "condition",
                left="{{ nodes.draft.text }}",
                operator="contains",
                right=needle,
            ),
            node("end", "end", output={"final": "{{ nodes.draft.text }}"}),
        ],
        "edges": [
            {"from": "start", "to": "draft"},
            {"from": "draft", "to": "check"},
            {"from": "check", "to": "end", "when": "true"},
            {"from": "check", "to": "draft", "when": "false", "back": True, "max_loops": max_loops},
        ],
    }


async def _steps(session, run_id: str) -> list[str]:
    rows = (
        await session.execute(
            select(WorkflowStep)
            .where(WorkflowStep.run_id == uuid.UUID(run_id))
            .order_by(WorkflowStep.sequence)
        )
    ).scalars()
    return [s.node_id for s in rows]


async def test_a_loop_runs_until_its_check_passes(client, tokens, builder, wf, session) -> None:
    wf.provider = SlowProvider(pause=0)
    workflow = await published(client, tokens, builder, redraft("DRAFT DRAFT DRAFT"))
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == "SUCCEEDED", result
    # Each pass reads the one before: "draft ", "draft DRAFT ", "draft DRAFT DRAFT ".
    assert result["output"] == {"final": "DRAFT DRAFT DRAFT "}  # the echo keeps the space
    assert await _steps(session, result["id"]) == [
        "start",
        "draft",
        "check",
        "draft",
        "check",
        "draft",
        "check",
        "end",
    ]


async def test_a_loop_that_never_passes_fails_the_run(client, tokens, builder, wf) -> None:
    wf.provider = SlowProvider(pause=0)
    workflow = await published(client, tokens, builder, redraft("NEVER", max_loops=2))
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == "FAILED"
    assert "went back to 'draft' 2 times" in result["error"]
