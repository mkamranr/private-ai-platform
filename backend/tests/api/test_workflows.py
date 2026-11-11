"""Workflows (E6): definitions, the engine, runs, approvals, schedules and webhooks.

The workflow service runs for real on the test database. Only its edges are replaced: a
fake model provider, a fake knowledge service, ``httpx.MockTransport`` for HTTP nodes and
fake tool executors. What is under test (graph order, branches, retries, pauses, frozen
permissions, scope) is the real code.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_workflow_service
from app.core.errors import ValidationError
from app.core.interfaces.llm import ChatCompletion, TokenUsage
from app.core.interfaces.tools import ToolResult, ToolType
from app.core.permissions import Permission as Perm
from app.models.agents import Tool
from app.models.audit import AuditAction, AuditLog
from app.models.teams import Team
from app.models.workflows import RunStatus, Workflow, WorkflowRun
from app.repositories.agents import ToolRepository
from app.repositories.audit import AuditRepository
from app.repositories.user import UserRepository
from app.repositories.workflows import (
    WorkflowApprovalRepository,
    WorkflowRepository,
    WorkflowRunRepository,
    WorkflowStepRepository,
    WorkflowVersionRepository,
)
from app.services import workflow_definition as wd
from app.services.audit import AuditService
from app.services.workflow_engine import WorkflowEngine
from app.services.workflows import WorkflowService, next_fire, template_definition
from tests.api.conftest import _user_with
from tests.conftest import auth_header

ALL = [Perm.WORKFLOW_VIEW, Perm.WORKFLOW_EDIT, Perm.WORKFLOW_EXECUTE, Perm.WORKFLOW_APPROVE]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeProvider:
    def __init__(self, reply: str = "Forty-two.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": [(m.role, m.content) for m in messages], **kwargs})
        return ChatCompletion(
            id="c",
            model=kwargs.get("model", "m"),
            content=self.reply,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
        )


class FakeKnowledge:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def get_base(self, ident: uuid.UUID) -> Any:
        return SimpleNamespace(id=ident, name="handbook")

    async def retrieve(self, bases, query, top_k=None):
        self.queries.append(query)
        hit = SimpleNamespace(
            text="Passwords are reset from the profile page.",
            document_name="it-guide.md",
            knowledge_base="handbook",
            location="page 2",
            score=0.91,
            rerank_score=None,
        )
        return SimpleNamespace(results=[hit], warnings=[])


class FakeExecutor:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.calls: list[Any] = []

    async def execute(self, invocation):
        self.calls.append(invocation)
        return ToolResult(
            success=self.success,
            content="created ticket 7" if self.success else "",
            duration_ms=1.0,
            data={"ticket": 7} if self.success else None,
            error=None if self.success else "upstream refused",
        )


class Http:
    """A MockTransport handler that answers from a script and records requests."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
async def wf(app, session: AsyncSession, settings) -> SimpleNamespace:
    ns = SimpleNamespace(
        provider=FakeProvider(),
        knowledge=FakeKnowledge(),
        executor=FakeExecutor(),
        http=Http(httpx.Response(200, json={"ok": True})),
        models=[],
        session=session,
    )

    async def provider_factory(model: str):
        ns.models.append(model)
        return ns.provider, f"served-{model}"

    def build() -> WorkflowService:
        engine = WorkflowEngine(
            settings,
            WorkflowStepRepository(session),
            WorkflowApprovalRepository(session),
            provider_factory=provider_factory,
            knowledge=ns.knowledge,
            tools=ToolRepository(session),
            tool_executors={ToolType.REST: ns.executor},
            http_transport=httpx.MockTransport(lambda r: ns.http(r)),
        )
        return WorkflowService(
            settings,
            WorkflowRepository(session),
            WorkflowVersionRepository(session),
            WorkflowRunRepository(session),
            WorkflowApprovalRepository(session),
            UserRepository(session),
            AuditService(AuditRepository(session)),
            engine,
        )

    # As in production (app.db.session): with autoflush on, a run's unflushed changes are
    # flushed by the next query, and a missing flush in the engine goes unnoticed.
    session.sync_session.autoflush = False
    app.dependency_overrides[get_workflow_service] = build
    ns.build = build
    ns.service = build()
    return ns


async def claim_until(service: WorkflowService, run_id: str) -> WorkflowRun | None:
    """Claim due runs until ``run_id`` comes up; None if it never does. The database is
    shared, so other due runs may be ahead of it in the queue; advancing them here is
    rolled back with the test."""
    target = uuid.UUID(run_id)
    for _ in range(100):
        claimed = await service.claim_due_run(dt.datetime.now(dt.UTC))
        if claimed is None or claimed.id == target:
            return claimed
    return None


@pytest.fixture
async def builder(session: AsyncSession, settings):
    return await _user_with(session, settings, ALL, name="wfbuilder")


@pytest.fixture
async def approver(session: AsyncSession, settings):
    return await _user_with(
        session, settings, [Perm.WORKFLOW_VIEW, Perm.WORKFLOW_APPROVE], name="wfapprover"
    )


# ---------------------------------------------------------------------------
# Definition helpers
# ---------------------------------------------------------------------------
def node(node_id: str, kind: str, **config: Any) -> dict[str, Any]:
    extra = {k: config.pop(k) for k in ("retry", "timeout_seconds") if k in config}
    return {"id": node_id, "type": kind, "name": node_id.title(), "config": config, **extra}


def edge(a: str, b: str, when: str | None = None) -> dict[str, Any]:
    return {"from": a, "to": b, **({"when": when} if when else {})}


def chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": list(nodes),
        "edges": [edge(a["id"], b["id"]) for a, b in itertools.pairwise(nodes)],
    }


async def create(client, tokens, user, definition: dict[str, Any] | None = None, **extra) -> dict:
    name = f"wf-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "display_name": name.upper(), **extra}
    if definition is not None:
        body["draft"] = definition
    response = await client.post("/api/v1/workflows", json=body, headers=auth_header(tokens, user))
    assert response.status_code == 201, response.text
    return response.json()


async def publish(client, tokens, user, workflow_id: str) -> dict:
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", json={}, headers=auth_header(tokens, user)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def run(client, tokens, user, workflow_id: str, payload: dict | None = None, **extra):
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": payload or {}, **extra},
        headers=auth_header(tokens, user),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def published(client, tokens, user, definition: dict[str, Any], **extra) -> dict:
    workflow = await create(client, tokens, user, definition, **extra)
    await publish(client, tokens, user, workflow["id"])
    return workflow


# ---------------------------------------------------------------------------
# Definitions (pure)
# ---------------------------------------------------------------------------
class TestDefinition:
    def test_starter_and_templates_are_well_formed(self) -> None:
        assert wd.validate(template_definition("blank")) == []
        # Templates leave the model, bases and URL for the operator: the draft says so.
        rag = {p.message for p in wd.validate(template_definition("rag_answer"))}
        assert rag == {"At least one knowledge base is required.", "model is required."}
        http = {p.message for p in wd.validate(template_definition("approval_http"))}
        assert http == {"url is required."}

    def test_problems_are_reported_per_node(self) -> None:
        definition = {
            "nodes": [
                node("start", "start"),
                node("ask", "llm"),
                node("wait", "delay", seconds=0),
                node("weird", "teleport"),
            ],
            "edges": [edge("start", "ask"), edge("ask", "wait"), edge("wait", "ghost")],
        }
        problems = {(p.node, p.message) for p in wd.validate(definition)}
        assert ("ask", "model is required.") in problems
        assert ("ask", "prompt is required.") in problems
        assert any(n == "wait" and "seconds" in m for n, m in problems)
        assert ("weird", "Unknown node type 'teleport'.") in problems
        assert any("ghost" in m for _, m in problems)

    def test_cycles_and_missing_start_are_refused(self) -> None:
        cyclic = {
            "nodes": [node("start", "start"), node("a", "transform"), node("b", "transform")],
            "edges": [edge("start", "a"), edge("a", "b"), edge("b", "a")],
        }
        assert any("cycle" in p.message.lower() for p in wd.validate(cyclic))
        with pytest.raises(ValidationError):
            wd.execution_order(cyclic)
        assert any("start" in p.message for p in wd.validate({"nodes": [], "edges": []}))

    def test_branch_labels_must_fit_the_node(self) -> None:
        definition = {
            "nodes": [
                node("start", "start"),
                node("check", "condition", operator="eq", left="{{ input.x }}", right=1),
                node("end", "end"),
            ],
            "edges": [edge("start", "check"), edge("check", "end", "approved")],
        }
        assert any("approved" in p.message for p in wd.validate(definition))

    def test_literal_urls_are_checked_against_the_outbound_policy(self) -> None:
        definition = chain(
            node("start", "start"),
            node("call", "http", url="http://169.254.169.254/latest"),
            node("end", "end"),
        )
        assert any(p.node == "call" for p in wd.validate(definition))

    def test_render_keeps_types_for_whole_templates(self) -> None:
        context = {"input": {"n": 3, "tags": ["a"]}, "nodes": {"x": {"text": "hi"}}}
        assert wd.render("{{ input.n }}", context) == 3
        assert wd.render("{{input.tags}}", context) == ["a"]
        assert wd.render("n={{ input.n }}, {{ nodes.x.text }}", context) == "n=3, hi"
        assert wd.render({"k": ["{{ nodes.x.text }}"]}, context) == {"k": ["hi"]}
        assert wd.render("{{ input.missing }}", context) is None

    @pytest.mark.parametrize(
        ("operator", "left", "right", "expected"),
        [
            ("eq", "1", 1, True),
            ("ne", "a", "b", True),
            ("gt", "10", 9, True),
            ("lte", 2, "2", True),
            ("contains", "hello world", "world", True),
            ("contains", ["a", "b"], "b", True),
            ("not_contains", "hello", "x", True),
            ("empty", "", None, True),
            ("empty", [], None, True),
            ("not_empty", {"a": 1}, None, True),
            ("gt", "abc", 1, False),
        ],
    )
    def test_evaluate(self, operator: str, left: Any, right: Any, expected: bool) -> None:
        assert wd.evaluate(operator, left, right) is expected

    def test_secret_headers_are_redacted(self) -> None:
        redacted = wd.redact_headers({"Authorization": "Bearer x", "X-Api-Key": "k", "Accept": "j"})
        assert redacted["Accept"] == "j"
        assert "x" not in str(redacted["Authorization"])
        assert redacted["X-Api-Key"] != "k"

    def test_cron(self) -> None:
        after = dt.datetime(2026, 11, 11, 6, 30, tzinfo=dt.UTC)
        assert next_fire("0 7 * * *", after) == dt.datetime(2026, 11, 11, 7, 0, tzinfo=dt.UTC)
        with pytest.raises(ValidationError):
            next_fire("every tuesday", after)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class TestExecution:
    async def test_rag_answer_end_to_end(self, client, tokens, builder, wf) -> None:
        definition = template_definition("rag_answer")
        nodes = wd.node_map(definition)
        base_id = str(uuid.uuid4())
        nodes["search"]["config"]["knowledge_base_ids"] = [base_id]
        nodes["answer"]["config"]["model"] = "llama-local"
        workflow = await published(client, tokens, builder, definition)

        result = await run(client, tokens, builder, workflow["id"], {"question": "Reset?"})

        assert result["status"] == RunStatus.SUCCEEDED, result
        assert result["output"]["answer"] == "Forty-two."
        assert result["output"]["sources"][0]["document"] == "it-guide.md"
        assert [s["node_id"] for s in result["steps"]] == ["start", "search", "answer", "end"]
        assert wf.knowledge.queries == ["Reset?"]
        assert wf.models == ["llama-local"]
        prompt = wf.provider.calls[0]["messages"][-1][1]
        assert "Passwords are reset" in prompt and "Question: Reset?" in prompt
        assert wf.provider.calls[0]["model"] == "served-llama-local"
        assert result["steps"][2]["output"]["usage"]["total_tokens"] == 13

    async def test_required_inputs(self, client, tokens, builder, wf) -> None:
        definition = chain(node("start", "start", required_inputs=["q"]), node("end", "end"))
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"], {})
        assert result["status"] == RunStatus.FAILED
        assert "Missing required input: q" in result["error"]

    async def test_condition_takes_one_branch_and_skips_the_other(
        self, client, tokens, builder, wf
    ) -> None:
        definition = {
            "nodes": [
                node("start", "start"),
                node("check", "condition", operator="gt", left="{{ input.amount }}", right=100),
                node("big", "transform", output={"route": "manager"}),
                node("small", "transform", output={"route": "auto"}),
                node("end", "end"),
            ],
            "edges": [
                edge("start", "check"),
                edge("check", "big", "true"),
                edge("check", "small", "false"),
                edge("big", "end"),
                edge("small", "end"),
            ],
        }
        workflow = await published(client, tokens, builder, definition)
        high = await run(client, tokens, builder, workflow["id"], {"amount": 500})
        low = await run(client, tokens, builder, workflow["id"], {"amount": 5})
        assert high["output"] == {"route": "manager"}
        assert low["output"] == {"route": "auto"}
        assert "small" not in [s["node_id"] for s in high["steps"]]
        assert high["steps"][1]["branch"] == "true"

    async def test_http_retries_then_succeeds(self, client, tokens, builder, wf) -> None:
        wf.http = Http(
            httpx.ConnectError("refused"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"id": 9}),
        )
        definition = chain(
            node("start", "start"),
            node(
                "call",
                "http",
                method="POST",
                url="http://erp.internal/api/orders",
                headers={"Authorization": "Bearer s3cret"},
                body={"sku": "{{ input.sku }}"},
                retry={"max_attempts": 3, "backoff_seconds": 0},
            ),
            node("end", "end", output={"order": "{{ nodes.call.body.id }}"}),
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"], {"sku": "A-1"})

        assert result["status"] == RunStatus.SUCCEEDED, result
        assert result["output"] == {"order": 9}
        call = result["steps"][1]
        assert call["attempts"] == 3
        assert len(wf.http.requests) == 3
        assert wf.http.requests[-1].read() == b'{"sku":"A-1"}'
        # The credential reached the service but not the trace.
        assert wf.http.requests[-1].headers["authorization"] == "Bearer s3cret"
        assert "s3cret" not in str(call["input"])

    async def test_error_edge_handles_a_failure(self, client, tokens, builder, wf) -> None:
        wf.http = Http(httpx.Response(500, text="boom"))
        definition = {
            "nodes": [
                node("start", "start"),
                node("call", "http", url="http://erp.internal/x"),
                node("ok", "end", output={"result": "sent"}),
                node(
                    "fallback", "end", output={"result": "queued", "why": "{{ nodes.call.error }}"}
                ),
            ],
            "edges": [
                edge("start", "call"),
                edge("call", "ok"),
                edge("call", "fallback", "error"),
            ],
        }
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"])
        assert result["status"] == RunStatus.SUCCEEDED
        assert result["output"]["result"] == "queued"
        assert "HTTP 500" in result["output"]["why"]
        assert result["steps"][1]["status"] == "FAILED"

    async def test_a_failure_without_an_error_edge_fails_the_run(
        self, client, tokens, builder, wf
    ) -> None:
        wf.http = Http(httpx.Response(404, text="nope"))
        definition = chain(
            node("start", "start"),
            node("call", "http", url="http://erp.internal/x"),
            node("end", "end"),
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"])
        assert result["status"] == RunStatus.FAILED
        assert "'call' failed" in result["error"] and "404" in result["error"]
        assert result["finished_at"]

    async def test_templated_url_is_checked_at_run_time(self, client, tokens, builder, wf) -> None:
        definition = chain(
            node("start", "start"), node("call", "http", url="{{ input.url }}"), node("end", "end")
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(
            client, tokens, builder, workflow["id"], {"url": "http://169.254.169.254/meta"}
        )
        assert result["status"] == RunStatus.FAILED
        assert wf.http.requests == []

    async def test_delay_pauses_and_resumes(self, client, tokens, builder, wf, session) -> None:
        definition = chain(
            node("start", "start"),
            node("wait", "delay", seconds=3600),
            node("end", "end", output={"done": True}),
        )
        workflow = await published(client, tokens, builder, definition)
        result = await run(client, tokens, builder, workflow["id"])
        assert result["status"] == RunStatus.WAITING
        assert result["resume_at"]

        # Not due yet: the worker finds nothing.
        assert await claim_until(wf.service, result["id"]) is None
        row = await session.get(WorkflowRun, uuid.UUID(result["id"]))
        assert row is not None
        row.resume_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()

        advanced = await claim_until(wf.service, result["id"])
        assert advanced is not None and advanced.status == RunStatus.SUCCEEDED
        detail = (
            await client.get(
                f"/api/v1/workflow-runs/{result['id']}", headers=auth_header(tokens, builder)
            )
        ).json()
        assert detail["output"] == {"done": True}
        assert [s["status"] for s in detail["steps"]] == ["SUCCEEDED"] * 3

    async def test_queued_runs_are_left_to_the_worker(self, client, tokens, builder, wf) -> None:
        workflow = await published(client, tokens, builder, template_definition("blank"))
        result = await run(client, tokens, builder, workflow["id"], {"a": 1}, wait=False)
        assert result["status"] == RunStatus.QUEUED and result["steps"] == []
        advanced = await claim_until(wf.service, result["id"])
        assert advanced is not None
        assert advanced.status == RunStatus.SUCCEEDED and advanced.output == {"a": 1}


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
def approval_flow(**config: Any) -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start"),
            node("review", "approval", message="Refund {{ input.amount }}?", **config),
            node("yes", "end", output={"refunded": True}),
            node("no", "end", output={"refunded": False}),
        ],
        "edges": [
            edge("start", "review"),
            edge("review", "yes", "approved"),
            edge("review", "no", "rejected"),
        ],
    }


class TestApprovals:
    async def test_approval_pauses_and_a_second_person_resumes(
        self, client, tokens, builder, approver, wf, session
    ) -> None:
        workflow = await published(client, tokens, builder, approval_flow())
        result = await run(client, tokens, builder, workflow["id"], {"amount": 40})
        assert result["status"] == RunStatus.WAITING_FOR_APPROVAL
        (approval,) = result["approvals"]
        assert approval["message"] == "Refund 40?" and approval["status"] == "PENDING"

        pending = (
            await client.get(
                "/api/v1/workflow-approvals/pending", headers=auth_header(tokens, approver)
            )
        ).json()
        mine = [p for p in pending if p["id"] == approval["id"]]
        assert mine and mine[0]["workflow_name"] == workflow["name"]

        # Whoever started the run cannot approve it.
        refused = await client.post(
            f"/api/v1/workflow-approvals/{approval['id']}/decide",
            json={"approved": True},
            headers=auth_header(tokens, builder),
        )
        assert refused.status_code == 403

        decided = await client.post(
            f"/api/v1/workflow-approvals/{approval['id']}/decide",
            json={"approved": True, "reason": "within policy"},
            headers=auth_header(tokens, approver),
        )
        assert decided.status_code == 200, decided.text
        body = decided.json()
        assert body["status"] == RunStatus.SUCCEEDED
        assert body["output"] == {"refunded": True}
        assert body["approvals"][0]["status"] == "APPROVED"
        assert body["approvals"][0]["reason"] == "within policy"

        again = await client.post(
            f"/api/v1/workflow-approvals/{approval['id']}/decide",
            json={"approved": False},
            headers=auth_header(tokens, approver),
        )
        assert again.status_code == 409

        actions = set(
            (
                await session.execute(
                    select(AuditLog.action).where(AuditLog.resource_id == result["id"])
                )
            ).scalars()
        )
        assert {AuditAction.WORKFLOW_RUN_STARTED, AuditAction.WORKFLOW_APPROVAL_GRANTED} <= actions

    async def test_rejection_takes_the_rejected_branch(
        self, client, tokens, builder, approver, wf
    ) -> None:
        workflow = await published(client, tokens, builder, approval_flow())
        result = await run(client, tokens, builder, workflow["id"], {"amount": 9000})
        decided = await client.post(
            f"/api/v1/workflow-approvals/{result['approvals'][0]['id']}/decide",
            json={"approved": False},
            headers=auth_header(tokens, approver),
        )
        assert decided.json()["output"] == {"refunded": False}

    async def test_self_approval_when_the_step_allows_it(self, client, tokens, builder, wf) -> None:
        workflow = await published(client, tokens, builder, approval_flow(allow_self_approval=True))
        result = await run(client, tokens, builder, workflow["id"], {"amount": 1})
        decided = await client.post(
            f"/api/v1/workflow-approvals/{result['approvals'][0]['id']}/decide",
            json={"approved": True},
            headers=auth_header(tokens, builder),
        )
        assert decided.status_code == 200 and decided.json()["status"] == RunStatus.SUCCEEDED

    async def test_cancel_expires_pending_approvals(self, client, tokens, builder, wf) -> None:
        workflow = await published(client, tokens, builder, approval_flow())
        result = await run(client, tokens, builder, workflow["id"], {"amount": 1})
        cancelled = await client.post(
            f"/api/v1/workflow-runs/{result['id']}/cancel", headers=auth_header(tokens, builder)
        )
        assert cancelled.status_code == 200
        body = cancelled.json()
        assert body["status"] == RunStatus.CANCELLED
        assert body["approvals"][0]["status"] == "EXPIRED"
        again = await client.post(
            f"/api/v1/workflow-runs/{result['id']}/cancel", headers=auth_header(tokens, builder)
        )
        assert again.status_code == 409


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
async def make_tool(session: AsyncSession, **overrides: Any) -> Tool:
    fields: dict[str, Any] = {
        "name": f"ticket-{uuid.uuid4().hex[:6]}",
        "display_name": "Create ticket",
        "description": "Opens a helpdesk ticket.",
        "type": ToolType.REST,
        "parameters_schema": {"type": "object"},
        # Something every workflow runner holds, unless a test asks for more.
        "required_permission": Perm.WORKFLOW_EXECUTE,
        "risk_level": "LOW",
        "endpoint": "http://helpdesk.internal/tickets",
        **overrides,
    }
    tool = Tool(**fields)
    session.add(tool)
    await session.flush()
    return tool


def tool_flow(tool: Tool) -> dict[str, Any]:
    return chain(
        node("start", "start"),
        node("ticket", "tool", tool_id=str(tool.id), arguments={"title": "{{ input.title }}"}),
        node("end", "end", output={"ticket": "{{ nodes.ticket.data.ticket }}"}),
    )


class TestTools:
    async def test_low_risk_tool_runs_directly(self, client, tokens, builder, wf, session) -> None:
        tool = await make_tool(session)
        workflow = await published(client, tokens, builder, tool_flow(tool))
        result = await run(client, tokens, builder, workflow["id"], {"title": "VPN down"})
        assert result["status"] == RunStatus.SUCCEEDED, result
        assert result["output"] == {"ticket": 7}
        (call,) = wf.executor.calls
        assert call.arguments == {"title": "VPN down"}
        assert call.agent_id == f"workflow:{workflow['id']}"

    async def test_high_risk_tool_waits_for_approval(
        self, client, tokens, builder, approver, wf, session
    ) -> None:
        tool = await make_tool(session, risk_level="HIGH")
        workflow = await published(client, tokens, builder, tool_flow(tool))
        result = await run(client, tokens, builder, workflow["id"], {"title": "Wipe laptop"})
        assert result["status"] == RunStatus.WAITING_FOR_APPROVAL
        approval = result["approvals"][0]
        assert approval["details"]["arguments"] == {"title": "Wipe laptop"}
        assert approval["allow_self_approval"] is False
        assert wf.executor.calls == []

        decided = await client.post(
            f"/api/v1/workflow-approvals/{approval['id']}/decide",
            json={"approved": True},
            headers=auth_header(tokens, approver),
        )
        assert decided.json()["status"] == RunStatus.SUCCEEDED
        assert len(wf.executor.calls) == 1

    async def test_rejected_tool_fails_the_run(
        self, client, tokens, builder, approver, wf, session
    ) -> None:
        tool = await make_tool(session, risk_level="CRITICAL")
        workflow = await published(client, tokens, builder, tool_flow(tool))
        result = await run(client, tokens, builder, workflow["id"], {"title": "x"})
        decided = await client.post(
            f"/api/v1/workflow-approvals/{result['approvals'][0]['id']}/decide",
            json={"approved": False},
            headers=auth_header(tokens, approver),
        )
        assert decided.json()["status"] == RunStatus.FAILED
        assert wf.executor.calls == []

    async def test_runs_carry_the_starters_permissions(
        self, client, tokens, builder, wf, session, settings
    ) -> None:
        tool = await make_tool(session, required_permission=Perm.TOOL_EXECUTE)
        workflow = await published(client, tokens, builder, tool_flow(tool))
        result = await run(client, tokens, builder, workflow["id"], {"title": "x"})
        assert result["status"] == RunStatus.FAILED
        assert "lacks 'tool.execute'" in result["error"]

        runner = await _user_with(
            session, settings, [Perm.WORKFLOW_EXECUTE, Perm.TOOL_EXECUTE], name="wfrunner"
        )
        result = await run(client, tokens, runner, workflow["id"], {"title": "x"})
        assert result["status"] == RunStatus.SUCCEEDED

    async def test_disabled_tool_and_failing_tool(
        self, client, tokens, builder, wf, session
    ) -> None:
        tool = await make_tool(session, enabled=False)
        workflow = await published(client, tokens, builder, tool_flow(tool))
        result = await run(client, tokens, builder, workflow["id"], {"title": "x"})
        assert "disabled" in result["error"]

        wf.executor.success = False
        tool.enabled = True
        await session.flush()
        result = await run(client, tokens, builder, workflow["id"], {"title": "x"})
        assert "upstream refused" in result["error"]


# ---------------------------------------------------------------------------
# Lifecycle over the API
# ---------------------------------------------------------------------------
class TestLifecycle:
    async def test_draft_publish_version_pause_archive(
        self, client, tokens, builder, wf, session
    ) -> None:
        headers = auth_header(tokens, builder)
        workflow = await create(client, tokens, builder, template="rag_answer")
        assert workflow["status"] == "DRAFT"
        assert workflow["problems"] and workflow["unpublished_changes"]

        # An invalid draft saves but does not publish.
        refused = await client.post(
            f"/api/v1/workflows/{workflow['id']}/publish", json={}, headers=headers
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["details"]["problems"]

        # Only a published workflow runs (a test run uses the draft).
        not_yet = await client.post(
            f"/api/v1/workflows/{workflow['id']}/runs", json={}, headers=headers
        )
        assert not_yet.status_code == 409

        fixed = template_definition("blank")
        patched = await client.patch(
            f"/api/v1/workflows/{workflow['id']}",
            json={"draft": fixed, "description": "Echo"},
            headers=headers,
        )
        assert patched.status_code == 200 and patched.json()["problems"] == []
        tested = await run(client, tokens, builder, workflow["id"], {"x": 1}, test=True)
        assert tested["trigger"] == "test" and tested["version"] is None

        published_once = await publish(client, tokens, builder, workflow["id"])
        assert published_once["status"] == "PUBLISHED"
        assert published_once["published_version"] == 1
        assert published_once["unpublished_changes"] is False
        await publish(client, tokens, builder, workflow["id"])
        versions = (
            await client.get(f"/api/v1/workflows/{workflow['id']}/versions", headers=headers)
        ).json()
        assert [v["version"] for v in versions] == [2, 1]

        result = await run(client, tokens, builder, workflow["id"], {"x": 2})
        assert result["version"] == 2 and result["output"] == {"x": 2}

        paused = await client.post(
            f"/api/v1/workflows/{workflow['id']}/status", json={"status": "PAUSED"}, headers=headers
        )
        assert paused.json()["status"] == "PAUSED"
        blocked = await client.post(
            f"/api/v1/workflows/{workflow['id']}/runs", json={}, headers=headers
        )
        assert blocked.status_code == 409

        listed = (await client.get("/api/v1/workflows", headers=headers)).json()
        mine = next(w for w in listed if w["id"] == workflow["id"])
        assert mine["last_run"]["id"] == result["id"]

        archived = await client.post(
            f"/api/v1/workflows/{workflow['id']}/status",
            json={"status": "ARCHIVED"},
            headers=headers,
        )
        assert archived.json()["status"] == "ARCHIVED"
        edit = await client.patch(
            f"/api/v1/workflows/{workflow['id']}", json={"description": "x"}, headers=headers
        )
        assert edit.status_code == 409

        deleted = await client.delete(f"/api/v1/workflows/{workflow['id']}", headers=headers)
        assert deleted.status_code == 204
        gone = await client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)
        assert gone.status_code == 404

    async def test_validate_endpoint(self, client, tokens, builder, wf) -> None:
        workflow = await create(client, tokens, builder)
        response = await client.post(
            f"/api/v1/workflows/{workflow['id']}/validate",
            json={},
            headers=auth_header(tokens, builder),
        )
        assert response.json() == {"valid": True, "problems": [], "order": ["start", "end"]}
        response = await client.post(
            f"/api/v1/workflows/{workflow['id']}/validate",
            json={"definition": {"nodes": [node("x", "llm")], "edges": []}},
            headers=auth_header(tokens, builder),
        )
        assert response.json()["valid"] is False

    async def test_duplicate_names_and_bad_names(self, client, tokens, builder, wf) -> None:
        workflow = await create(client, tokens, builder)
        again = await client.post(
            "/api/v1/workflows",
            json={"name": workflow["name"], "display_name": "x"},
            headers=auth_header(tokens, builder),
        )
        assert again.status_code == 409
        bad = await client.post(
            "/api/v1/workflows",
            json={"name": "Has Spaces", "display_name": "x"},
            headers=auth_header(tokens, builder),
        )
        assert bad.status_code == 422

    async def test_delete_is_refused_while_runs_are_active(
        self, client, tokens, builder, wf
    ) -> None:
        workflow = await published(client, tokens, builder, approval_flow())
        await run(client, tokens, builder, workflow["id"], {"amount": 1})
        response = await client.delete(
            f"/api/v1/workflows/{workflow['id']}", headers=auth_header(tokens, builder)
        )
        assert response.status_code == 409

    async def test_test_runs_need_edit(
        self, client, tokens, builder, wf, session, settings
    ) -> None:
        workflow = await published(client, tokens, builder, template_definition("blank"))
        runner = await _user_with(session, settings, [Perm.WORKFLOW_EXECUTE], name="wfrun")
        response = await client.post(
            f"/api/v1/workflows/{workflow['id']}/runs",
            json={"test": True},
            headers=auth_header(tokens, runner),
        )
        assert response.status_code == 403
        assert (await run(client, tokens, runner, workflow["id"]))["status"] == "SUCCEEDED"

    async def test_node_types(self, client, tokens, builder, wf) -> None:
        types = (
            await client.get("/api/v1/workflows/node-types", headers=auth_header(tokens, builder))
        ).json()
        by_type = {t["type"]: t for t in types}
        assert set(by_type) == set(wd.NODE_TYPES)
        assert by_type["condition"]["branches"] == ["false", "true"]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
class TestTriggers:
    async def test_webhook(self, client, tokens, builder, wf, session) -> None:
        workflow = await published(client, tokens, builder, template_definition("blank"))
        url = f"/api/v1/workflow-hooks/{workflow['id']}"

        # No webhook yet: the same 401 as a wrong token.
        assert (
            await client.post(url, json={}, headers={"X-Workflow-Token": "x"})
        ).status_code == 401

        rotated = await client.post(
            f"/api/v1/workflows/{workflow['id']}/webhook", headers=auth_header(tokens, builder)
        )
        token = rotated.json()["token"]
        assert token.startswith("wfh_")
        assert rotated.json()["url"].endswith(url)

        for headers in ({}, {"X-Workflow-Token": "wfh_wrong"}):
            assert (await client.post(url, json={}, headers=headers)).status_code == 401
        missing = f"/api/v1/workflow-hooks/{uuid.uuid4()}"
        assert (
            await client.post(missing, json={}, headers={"X-Workflow-Token": token})
        ).status_code == 401

        accepted = await client.post(url, json={"order": 5}, headers={"X-Workflow-Token": token})
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert body["status"] == "QUEUED" and body["trigger"] == "webhook"
        row = await session.get(WorkflowRun, uuid.UUID(body["id"]))
        assert row is not None and row.started_by is None
        # Runs as the publisher, whose permissions were frozen onto it.
        assert Perm.WORKFLOW_EDIT in row.user_permissions

        # The token is stored hashed, and rotation replaces it.
        stored = await session.get(Workflow, uuid.UUID(workflow["id"]))
        assert stored is not None and token not in str(stored.webhook_token_hash)
        await client.post(
            f"/api/v1/workflows/{workflow['id']}/webhook", headers=auth_header(tokens, builder)
        )
        assert (
            await client.post(url, json={}, headers={"X-Workflow-Token": token})
        ).status_code == 401

        detail = await client.delete(
            f"/api/v1/workflows/{workflow['id']}/webhook", headers=auth_header(tokens, builder)
        )
        assert detail.status_code == 200

    async def test_schedule_fires_once_and_moves_on(
        self, client, tokens, builder, wf, session
    ) -> None:
        workflow = await published(client, tokens, builder, template_definition("blank"))
        response = await client.patch(
            f"/api/v1/workflows/{workflow['id']}",
            json={"schedule_cron": "*/5 * * * *", "schedule_input": {"source": "cron"}},
            headers=auth_header(tokens, builder),
        )
        assert response.status_code == 200
        assert response.json()["next_run_at"]

        row = await session.get(Workflow, uuid.UUID(workflow["id"]))
        assert row is not None
        row.next_run_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        await session.flush()

        now = dt.datetime.now(dt.UTC)
        fired = await wf.service.fire_due_schedule(now)
        assert fired is not None and fired.workflow_id == row.id
        assert fired.trigger == "schedule" and fired.input == {"source": "cron"}
        assert row.next_run_at is not None and row.next_run_at > now
        # Nothing else is due for this workflow now.
        again = await wf.service.fire_due_schedule(now)
        assert again is None or again.workflow_id != row.id

    async def test_a_bad_cron_is_refused(self, client, tokens, builder, wf) -> None:
        workflow = await create(client, tokens, builder)
        response = await client.patch(
            f"/api/v1/workflows/{workflow['id']}",
            json={"schedule_cron": "61 * * * *"},
            headers=auth_header(tokens, builder),
        )
        assert response.status_code == 422

    async def test_a_paused_workflow_does_not_schedule(self, client, tokens, builder, wf) -> None:
        workflow = await published(client, tokens, builder, template_definition("blank"))
        headers = auth_header(tokens, builder)
        await client.patch(
            f"/api/v1/workflows/{workflow['id']}",
            json={"schedule_cron": "0 * * * *"},
            headers=headers,
        )
        paused = await client.post(
            f"/api/v1/workflows/{workflow['id']}/status", json={"status": "PAUSED"}, headers=headers
        )
        assert paused.json()["next_run_at"] is None


# ---------------------------------------------------------------------------
# Scope and permissions
# ---------------------------------------------------------------------------
class TestScope:
    async def test_another_teams_workflow_is_invisible(
        self, client, tokens, builder, wf, session, superuser
    ) -> None:
        team = Team(name=f"Ops {uuid.uuid4().hex[:6]}", slug=f"ops-{uuid.uuid4().hex[:6]}")
        session.add(team)
        await session.flush()
        workflow = await published(client, tokens, superuser, approval_flow(), team_id=str(team.id))
        result = await run(client, tokens, superuser, workflow["id"], {"amount": 1})

        headers = auth_header(tokens, builder)
        assert (
            await client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/workflow-runs/{result['id']}", headers=headers)
        ).status_code == 404
        listed = (await client.get("/api/v1/workflows", headers=headers)).json()
        assert workflow["id"] not in {w["id"] for w in listed}
        runs = (await client.get("/api/v1/workflow-runs", headers=headers)).json()
        assert result["id"] not in {r["id"] for r in runs}
        pending = (await client.get("/api/v1/workflow-approvals/pending", headers=headers)).json()
        assert result["approvals"][0]["id"] not in {p["id"] for p in pending}
        decide = await client.post(
            f"/api/v1/workflow-approvals/{result['approvals'][0]['id']}/decide",
            json={"approved": True},
            headers=headers,
        )
        assert decide.status_code == 404

    async def test_viewer_cannot_edit_or_run(self, client, tokens, builder, wf, session, settings):
        workflow = await published(client, tokens, builder, template_definition("blank"))
        viewer = await _user_with(session, settings, [Perm.WORKFLOW_VIEW], name="wfviewer")
        headers = auth_header(tokens, viewer)
        assert (
            await client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)
        ).status_code == 200
        assert (
            await client.patch(
                f"/api/v1/workflows/{workflow['id']}", json={"description": "x"}, headers=headers
            )
        ).status_code == 403
        assert (
            await client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={}, headers=headers)
        ).status_code == 403
