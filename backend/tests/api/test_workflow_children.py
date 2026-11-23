"""Sub-workflows and loops (E71): a ``workflow`` step runs another published workflow as
a run of its own, a ``foreach`` step runs one per item, and a child that pauses leaves the
step waiting until it finishes.

Uses the real engine and service on the test database, with the fakes from
``test_workflows.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.models.teams import Team
from app.models.workflows import RunStatus, WorkflowApproval, WorkflowRun
from app.services import workflow_definition as wd
from tests.api import test_workflows as base
from tests.api.test_workflows import approval_flow, chain, create, edge, node, published, run
from tests.conftest import auth_header

pytestmark = pytest.mark.asyncio

# The fixtures of test_workflows.py, shared.
wf = base.wf
builder = base.builder
approver = base.approver

ECHO = chain(node("start", "start"), node("done", "end", output={"echo": "{{ input.n }}"}))


def calls(workflow_id: str, **config: Any) -> dict[str, Any]:
    return chain(
        node("start", "start"),
        node("sub", "workflow", workflow_id=workflow_id, input={"n": "{{ input.n }}"}, **config),
        node("done", "end", output={"got": "{{ nodes.sub.output }}"}),
    )


def loops(workflow_id: str, **config: Any) -> dict[str, Any]:
    return chain(
        node("start", "start"),
        node("each", "foreach", items="{{ input.list }}", workflow_id=workflow_id, **config),
        node("done", "end", output={"all": "{{ nodes.each.results }}"}),
    )


async def children_of(session, run_id: str) -> list[WorkflowRun]:
    rows = await session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.parent_run_id == uuid.UUID(run_id))
        .order_by(WorkflowRun.created_at)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars())


async def get_run(client, tokens, user, run_id: str) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/workflow-runs/{run_id}", headers=auth_header(tokens, user)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def decide(client, tokens, user, approval_id: str, approved: bool = True) -> dict:
    response = await client.post(
        f"/api/v1/workflow-approvals/{approval_id}/decide",
        json={"approved": approved},
        headers=auth_header(tokens, user),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def pending_approval(session, run_id: str) -> WorkflowApproval:
    return (
        await session.execute(
            select(WorkflowApproval).where(
                WorkflowApproval.run_id == uuid.UUID(run_id), WorkflowApproval.status == "PENDING"
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Definitions (pure)
# ---------------------------------------------------------------------------
class TestDefinition:
    def test_both_steps_validate(self) -> None:
        ident = str(uuid.uuid4())
        assert wd.validate(calls(ident)) == []
        assert wd.validate(loops(ident, input={"n": "{{ item }}", "i": "{{ index }}"})) == []
        assert wd.node_outputs({"type": "foreach"}) == ("results", "count", "run_ids")

    def test_a_workflow_id_is_required_and_an_id(self) -> None:
        messages = [p.message for p in wd.validate(calls("not-an-id"))]
        assert "workflow_id is a workflow id." in messages
        bare = chain(node("start", "start"), node("sub", "workflow"))
        assert "A workflow is required." in [p.message for p in wd.validate(bare)]

    def test_item_only_inside_a_foreach_input(self) -> None:
        ident = str(uuid.uuid4())
        stray = chain(
            node("start", "start"),
            node("each", "foreach", items="{{ item }}", workflow_id=ident),
        )
        (problem,) = wd.validate(stray)
        assert "only in a foreach step's input" in problem.message
        elsewhere = chain(node("start", "start"), node("done", "end", output="{{ index }}"))
        assert wd.validate(elsewhere)

    def test_referenced_workflows(self) -> None:
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        definition = {
            "nodes": [
                node("start", "start"),
                node("one", "workflow", workflow_id=a),
                node("many", "foreach", items=[1], workflow_id=b),
            ],
            "edges": [edge("start", "one"), edge("one", "many")],
        }
        assert sorted(wd.referenced_workflows(definition)) == sorted([a, b])


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------
class TestSubWorkflow:
    async def test_the_child_runs_and_its_output_is_the_steps(
        self, client, tokens, builder, wf, session
    ) -> None:
        child = await published(client, tokens, builder, ECHO)
        parent = await published(client, tokens, builder, calls(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"n": 7})
        assert result["status"] == RunStatus.SUCCEEDED, result
        assert result["output"] == {"got": {"echo": 7}}

        (row,) = await children_of(session, result["id"])
        assert row.trigger == "workflow" and row.depth == 1
        assert row.parent_node_id == "sub" and row.workflow_id == uuid.UUID(child["id"])
        assert row.started_by == builder.id and row.version == 1
        detail = await get_run(client, tokens, builder, str(row.id))
        assert detail["parent_run_id"] == result["id"]

    async def test_a_failed_child_fails_the_step_or_takes_its_error_branch(
        self, client, tokens, builder, wf
    ) -> None:
        strict = chain(node("start", "start", required_inputs=["needed"]), node("done", "end"))
        child = await published(client, tokens, builder, strict)
        parent = await published(client, tokens, builder, calls(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"n": 1})
        assert result["status"] == RunStatus.FAILED
        assert "sub-workflow run" in result["error"] and "Missing required input" in result["error"]

        handled = calls(child["id"])
        handled["nodes"].append(node("oops", "end", output={"why": "{{ nodes.sub.error }}"}))
        handled["edges"].append(edge("sub", "oops", "error"))
        parent = await published(client, tokens, builder, handled)
        result = await run(client, tokens, builder, parent["id"], {"n": 1})
        assert result["status"] == RunStatus.SUCCEEDED
        assert "Missing required input" in result["output"]["why"]

    async def test_a_paused_child_leaves_the_step_waiting_until_it_ends(
        self, client, tokens, builder, approver, wf, session
    ) -> None:
        child = await published(client, tokens, builder, approval_flow())
        parent = await published(client, tokens, builder, calls(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"n": 3})
        assert result["status"] == RunStatus.WAITING and result["resume_at"] is None

        (row,) = await children_of(session, result["id"])
        assert row.status == RunStatus.WAITING_FOR_APPROVAL
        approval = await pending_approval(session, str(row.id))
        decided = await decide(client, tokens, approver, str(approval.id))
        assert decided["status"] == RunStatus.SUCCEEDED

        after = await get_run(client, tokens, builder, result["id"])
        assert after["status"] == RunStatus.SUCCEEDED, after
        assert after["output"] == {"got": {"refunded": True}}

    async def test_publishing_checks_the_sub_workflow(self, client, tokens, builder, wf) -> None:
        draft = await create(client, tokens, builder, ECHO)
        parent = await create(client, tokens, builder, calls(draft["id"]))
        response = await client.post(
            f"/api/v1/workflows/{parent['id']}/publish",
            json={},
            headers=auth_header(tokens, builder),
        )
        assert response.status_code == 422, response.text
        assert "Publish it first" in response.text

        itself = await create(client, tokens, builder, ECHO)
        response = await client.patch(
            f"/api/v1/workflows/{itself['id']}",
            json={"draft": calls(itself["id"])},
            headers=auth_header(tokens, builder),
        )
        assert response.status_code == 200, response.text
        response = await client.post(
            f"/api/v1/workflows/{itself['id']}/publish",
            json={},
            headers=auth_header(tokens, builder),
        )
        assert response.status_code == 422 and "cannot run itself" in response.text

    async def test_nesting_stops_at_the_limit(self, client, tokens, builder, wf) -> None:
        inner = await published(client, tokens, builder, ECHO)
        for _ in range(wd.MAX_DEPTH):
            inner = await published(client, tokens, builder, calls(inner["id"]))
        # MAX_DEPTH levels of sub-workflows below the outermost run: allowed.
        result = await run(client, tokens, builder, inner["id"], {"n": 1})
        assert result["status"] == RunStatus.SUCCEEDED, result

        outer = await published(client, tokens, builder, calls(inner["id"]))
        result = await run(client, tokens, builder, outer["id"], {"n": 1})
        assert result["status"] == RunStatus.FAILED
        assert f"nest at most {wd.MAX_DEPTH} deep" in result["error"]

    async def test_a_shared_workflow_cannot_run_a_teams(
        self, client, tokens, wf, session, superuser
    ) -> None:
        team = Team(name=f"Ops {uuid.uuid4().hex[:6]}", slug=f"ops-{uuid.uuid4().hex[:6]}")
        session.add(team)
        await session.flush()
        child = await published(client, tokens, superuser, ECHO, team_id=str(team.id))
        name = f"wf-{uuid.uuid4().hex[:8]}"
        response = await client.post(
            "/api/v1/workflows",
            json={"name": name, "display_name": name, "draft": calls(child["id"])},
            headers=auth_header(tokens, superuser),
        )
        assert response.status_code == 422, response.text
        assert f"workflow {child['id']}" in response.text


class TestForEach:
    async def test_one_child_per_item_in_order(self, client, tokens, builder, wf, session) -> None:
        child = await published(client, tokens, builder, ECHO)
        parent = await published(
            client, tokens, builder, loops(child["id"], input={"n": "{{ item.v }}-{{ index }}"})
        )
        result = await run(
            client, tokens, builder, parent["id"], {"list": [{"v": "a"}, {"v": "b"}, {"v": "c"}]}
        )
        assert result["status"] == RunStatus.SUCCEEDED, result
        assert result["output"] == {"all": [{"echo": "a-0"}, {"echo": "b-1"}, {"echo": "c-2"}]}
        rows = await children_of(session, result["id"])
        # One transaction, so created_at ties: the output above shows the order.
        assert sorted(r.input["n"] for r in rows) == ["a-0", "b-1", "c-2"]
        assert result["steps"][-2]["output"]["run_ids"] == [
            str(r.id) for r in sorted(rows, key=lambda r: r.input["n"])
        ]

    async def test_the_item_is_the_input_by_default(self, client, tokens, builder, wf) -> None:
        child = await published(
            client,
            tokens,
            builder,
            chain(node("start", "start"), node("done", "end", output="{{ input.item }}")),
        )
        parent = await published(client, tokens, builder, loops(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"list": [5, 6]})
        assert result["output"] == {"all": [{"value": 5}, {"value": 6}]}

    async def test_too_many_items_or_not_a_list_fail(self, client, tokens, builder, wf) -> None:
        child = await published(client, tokens, builder, ECHO)
        parent = await published(client, tokens, builder, loops(child["id"], max_items=2))
        result = await run(client, tokens, builder, parent["id"], {"list": [1, 2, 3]})
        assert result["status"] == RunStatus.FAILED
        assert "3 items; this step takes at most 2" in result["error"]
        result = await run(client, tokens, builder, parent["id"], {"list": "abc"})
        assert "items is not a list" in result["error"]

    async def test_paused_items_continue_one_after_another(
        self, client, tokens, builder, approver, wf, session
    ) -> None:
        child = await published(client, tokens, builder, approval_flow())
        parent = await published(
            client, tokens, builder, loops(child["id"], input={"amount": "{{ item }}"})
        )
        result = await run(client, tokens, builder, parent["id"], {"list": [10, 20]})
        assert result["status"] == RunStatus.WAITING

        (first,) = await children_of(session, result["id"])
        first_approval = await pending_approval(session, str(first.id))
        await decide(client, tokens, approver, str(first_approval.id))
        assert (await get_run(client, tokens, builder, result["id"]))["status"] == RunStatus.WAITING

        rows = await children_of(session, result["id"])
        assert {r.id for r in rows} > {first.id}
        (second,) = [r for r in rows if r.id != first.id]
        assert second.input == {"amount": 20}
        approval = await pending_approval(session, str(second.id))
        await decide(client, tokens, approver, str(approval.id), approved=False)

        after = await get_run(client, tokens, builder, result["id"])
        assert after["status"] == RunStatus.SUCCEEDED, after
        assert after["output"] == {"all": [{"refunded": True}, {"refunded": False}]}


class TestCancel:
    async def test_cancelling_the_parent_cancels_its_child(
        self, client, tokens, builder, wf, session
    ) -> None:
        child = await published(client, tokens, builder, approval_flow())
        parent = await published(client, tokens, builder, calls(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"n": 1})
        (row,) = await children_of(session, result["id"])
        approval = await pending_approval(session, str(row.id))

        response = await client.post(
            f"/api/v1/workflow-runs/{result['id']}/cancel", headers=auth_header(tokens, builder)
        )
        assert response.status_code == 200, response.text
        (row,) = await children_of(session, result["id"])
        assert row.status == RunStatus.CANCELLED and "parent run was cancelled" in row.error
        await session.refresh(approval)
        assert approval.status == "EXPIRED"

    async def test_cancelling_the_child_fails_the_parents_step(
        self, client, tokens, builder, wf, session
    ) -> None:
        child = await published(client, tokens, builder, approval_flow())
        parent = await published(client, tokens, builder, calls(child["id"]))
        result = await run(client, tokens, builder, parent["id"], {"n": 1})
        (row,) = await children_of(session, result["id"])
        response = await client.post(
            f"/api/v1/workflow-runs/{row.id}/cancel", headers=auth_header(tokens, builder)
        )
        assert response.status_code == 200, response.text
        after = await get_run(client, tokens, builder, result["id"])
        assert after["status"] == RunStatus.FAILED
        assert "cancelled" in after["error"]
