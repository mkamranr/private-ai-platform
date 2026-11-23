"""Parallel branches (E69): steps whose inputs are ready run at the same time, a pause on
one branch doesn't stop another, and an end step waits for every branch.

Uses the real engine and service on the test database, with the fakes from
``test_workflows.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.interfaces.llm import ChatCompletion, TokenUsage
from app.models.workflows import RunStatus, WorkflowRun, WorkflowStep
from tests.api import test_workflows as base
from tests.api.test_workflows import Http, claim_until, edge, node, published, run
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

# The fixtures of test_workflows.py, shared.
wf = base.wf
builder = base.builder
approver = base.approver


class SlowProvider:
    """Answers after a pause, counting how many calls are in flight at once."""

    def __init__(self, pause: float = 0.2) -> None:
        self.pause = pause
        self.in_flight = 0
        self.most = 0

    async def chat(self, messages, **kwargs):
        self.in_flight += 1
        self.most = max(self.most, self.in_flight)
        try:
            await asyncio.sleep(self.pause)
        finally:
            self.in_flight -= 1
        return ChatCompletion(
            id="c",
            model=kwargs.get("model", "m"),
            content=messages[-1].content.upper(),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def graph(nodes: list[dict[str, Any]], *edges: tuple) -> dict[str, Any]:
    return {"nodes": nodes, "edges": [edge(*e) for e in edges]}


async def steps_of(session, run_id: str) -> dict[str, str]:
    rows = (
        await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == uuid.UUID(run_id)))
    ).scalars()
    return {s.node_id: s.status for s in rows}


async def test_two_branches_run_at_the_same_time(client, tokens, builder, wf) -> None:
    wf.provider = SlowProvider()
    definition = graph(
        [
            node("start", "start"),
            node("left", "llm", model="m", prompt="left"),
            node("right", "llm", model="m", prompt="right"),
            node(
                "join",
                "transform",
                output={"both": "{{ nodes.left.text }}+{{ nodes.right.text }}"},
            ),
            node("end", "end"),
        ],
        ("start", "left"),
        ("start", "right"),
        ("left", "join"),
        ("right", "join"),
        ("join", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.SUCCEEDED, result
    assert result["output"] == {"both": "LEFT+RIGHT"}
    assert wf.provider.most == 2  # both model calls were in flight together
    # Each step has its own sequence number, as before.
    assert sorted(s["sequence"] for s in result["steps"]) == list(range(1, 6))


async def test_a_chain_still_runs_one_step_at_a_time(client, tokens, builder, wf) -> None:
    wf.provider = SlowProvider(pause=0.01)
    definition = graph(
        [
            node("start", "start"),
            node("a", "llm", model="m", prompt="a"),
            node("b", "llm", model="m", prompt="{{ nodes.a.text }}b"),
            node("end", "end", output={"v": "{{ nodes.b.text }}"}),
        ],
        ("start", "a"),
        ("a", "b"),
        ("b", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["output"] == {"v": "AB"} and wf.provider.most == 1


async def test_a_step_waits_for_an_earlier_step_it_reads(client, tokens, builder, wf) -> None:
    """``late`` follows only ``start`` but reads ``early``: it still sees its value, as it
    did when steps ran one at a time."""
    wf.provider = SlowProvider(pause=0.05)
    definition = graph(
        [
            node("start", "start"),
            node("early", "llm", model="m", prompt="early"),
            node("late", "transform", output={"saw": "{{ nodes.early.text }}"}),
            node("end", "end", output={"saw": "{{ nodes.late.saw }}"}),
        ],
        ("start", "early"),
        ("start", "late"),
        ("early", "end"),
        ("late", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["output"] == {"saw": "EARLY"}


async def test_an_approval_on_one_branch_lets_the_other_run(
    client, tokens, builder, approver, wf, session
) -> None:
    wf.http = Http(httpx.Response(200, json={"stock": 3}))
    definition = graph(
        [
            node("start", "start"),
            node("review", "approval", message="Go?"),
            node("fetch", "http", url="http://erp.internal/stock"),
            node("end", "end", output={"stock": "{{ nodes.fetch.body.stock }}"}),
        ],
        ("start", "review"),
        ("start", "fetch"),
        ("review", "end", "approved"),
        ("fetch", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL
    assert await steps_of(session, result["id"]) == {
        "start": "SUCCEEDED",
        "review": "WAITING",
        "fetch": "SUCCEEDED",
    }
    (approval,) = result["approvals"]
    decided = await client.post(
        f"/api/v1/workflow-approvals/{approval['id']}/decide",
        json={"approved": True},
        headers=auth_header(tokens, approver),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == RunStatus.SUCCEEDED
    assert decided.json()["output"] == {"stock": 3}


async def test_two_delays_wait_on_their_own_clocks(client, tokens, builder, wf, session) -> None:
    definition = graph(
        [
            node("start", "start"),
            node("short", "delay", seconds=60),
            node("long", "delay", seconds=3600),
            node("end", "end", output={"done": True}),
        ],
        ("start", "short"),
        ("start", "long"),
        ("short", "end"),
        ("long", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.WAITING
    row = await session.get(WorkflowRun, uuid.UUID(result["id"]))
    assert row is not None
    nodes = row.state["nodes"]
    short_due = dt.datetime.fromisoformat(nodes["short"]["resume_at"])
    long_due = dt.datetime.fromisoformat(nodes["long"]["resume_at"])
    assert row.resume_at == short_due < long_due

    # The short wait is over: the run wakes, the long one is still waiting.
    past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    row.resume_at = past
    row.state = {
        **row.state,
        "nodes": {**nodes, "short": {**nodes["short"], "resume_at": past.isoformat()}},
    }
    await session.flush()
    advanced = await claim_until(wf.service, result["id"])
    assert advanced is not None and advanced.status == RunStatus.WAITING
    assert advanced.resume_at == long_due
    assert advanced.state["nodes"]["short"]["status"] == "succeeded"

    advanced.resume_at = past
    advanced.state = {
        **advanced.state,
        "nodes": {
            **advanced.state["nodes"],
            "long": {**advanced.state["nodes"]["long"], "resume_at": past.isoformat()},
        },
    }
    await session.flush()
    finished = await claim_until(wf.service, result["id"])
    assert finished is not None and finished.status == RunStatus.SUCCEEDED
    assert finished.output == {"done": True}


async def test_a_delay_finishes_while_another_branch_awaits_approval(
    client, tokens, builder, wf, session
) -> None:
    definition = graph(
        [
            node("start", "start"),
            node("review", "approval", message="Go?"),
            node("wait", "delay", seconds=60),
            node("after", "transform", output={"waited": True}),
            node("end", "end"),
        ],
        ("start", "review"),
        ("start", "wait"),
        ("wait", "after"),
        ("review", "end", "approved"),
        ("after", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.WAITING_FOR_APPROVAL and result["resume_at"]

    row = await session.get(WorkflowRun, uuid.UUID(result["id"]))
    past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    nodes = row.state["nodes"]
    row.resume_at = past
    row.state = {
        **row.state,
        "nodes": {**nodes, "wait": {**nodes["wait"], "resume_at": past.isoformat()}},
    }
    await session.flush()
    # The worker picks it up although it awaits an approval: the delay branch carries on.
    advanced = await claim_until(wf.service, result["id"])
    assert advanced is not None and advanced.status == RunStatus.WAITING_FOR_APPROVAL
    assert advanced.resume_at is None
    assert advanced.state["nodes"]["after"]["status"] == "succeeded"
    # Nothing more to do until someone decides.
    assert await claim_until(wf.service, result["id"]) is None


async def test_a_failing_branch_fails_the_run_once_the_other_is_done(
    client, tokens, builder, wf, session
) -> None:
    wf.provider = SlowProvider(pause=0.05)
    wf.http = Http(httpx.Response(500, text="down"))
    definition = graph(
        [
            node("start", "start"),
            node("call", "http", url="http://erp.internal/x"),
            node("ask", "llm", model="m", prompt="hi"),
            node("end", "end"),
        ],
        ("start", "call"),
        ("start", "ask"),
        ("call", "end"),
        ("ask", "end"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.FAILED
    assert "'call'" in result["error"]
    steps = await steps_of(session, result["id"])
    assert steps["call"] == "FAILED" and steps["ask"] == "SUCCEEDED" and "end" not in steps


async def test_an_end_waits_for_every_branch(client, tokens, builder, wf, session) -> None:
    """A branch straight to the end no longer finishes the run before its sibling."""
    wf.provider = SlowProvider(pause=0.05)
    definition = graph(
        [
            node("start", "start"),
            node("done", "end", output={"done": True}),
            node("audit", "llm", model="m", prompt="log it"),
        ],
        ("start", "done"),
        ("start", "audit"),
    )
    workflow = await published(client, tokens, builder, definition)
    result = await run(client, tokens, builder, workflow["id"])
    assert result["status"] == RunStatus.SUCCEEDED and result["output"] == {"done": True}
    assert (await steps_of(session, result["id"]))["audit"] == "SUCCEEDED"


async def test_node_types_carry_their_schema(client, tokens, builder) -> None:
    response = await client.get(
        "/api/v1/workflows/node-types", headers=auth_header(tokens, builder)
    )
    assert response.status_code == 200
    by_type = {t["type"]: t for t in response.json()}
    assert by_type["retrieval"]["outputs"] == ["passages", "count", "context", "warnings"]
    assert by_type["http"]["timed"] is True and by_type["condition"]["timed"] is False
    method = next(f for f in by_type["http"]["fields"] if f["key"] == "method")
    assert method["kind"] == "enum" and "POST" in method["options"]
    assert by_type["transform"]["outputs"] is None
