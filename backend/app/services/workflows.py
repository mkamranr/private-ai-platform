"""Workflow lifecycle, triggers, runs and approvals (E6).

The engine (`workflow_engine`) executes a run; this service decides whether a run may
start, with which definition and whose permissions, and records every change.

**Lifecycle.** DRAFT → PUBLISHED ⇄ PAUSED → ARCHIVED. Publishing validates the draft and
freezes it as the next version. Only a published workflow accepts manual, API, schedule
and webhook runs; any workflow can test-run its draft.

**Whose permissions a run has.** A run started by a person carries that person's
permissions, frozen at start. Schedule and webhook runs have no person, so they carry the
permissions of whoever published the version they execute: publishing a workflow with a
schedule is the act that authorises it to run unattended.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.config.settings import Settings
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.permissions import PERMISSION_CATALOGUE
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.workflows import (
    ApprovalStatus,
    RunStatus,
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowStatus,
    WorkflowVersion,
)
from app.repositories.user import UserRepository
from app.repositories.workflows import (
    WorkflowApprovalRepository,
    WorkflowRepository,
    WorkflowRunRepository,
    WorkflowVersionRepository,
)
from app.services import workflow_definition as wd
from app.services.audit import AuditService
from app.services.workflow_engine import WorkflowEngine

log = get_logger(__name__)

TRIGGERS = frozenset({"manual", "api", "schedule", "webhook", "test"})
UPDATABLE = frozenset({"display_name", "description", "draft", "schedule_cron", "schedule_input"})
#: Input larger than this is refused: run input is copied into every run row.
MAX_INPUT_BYTES = 64 * 1024


def starter_definition() -> dict[str, Any]:
    """What a new workflow begins as: start → end, echoing the input."""
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "name": "Start",
                "config": {},
                "position": {"x": 0, "y": 0},
            },
            {
                "id": "end",
                "type": "end",
                "name": "End",
                "config": {"output": "{{ input }}"},
                "position": {"x": 1, "y": 0},
            },
        ],
        "edges": [{"from": "start", "to": "end"}],
    }


def _node(node_id: str, kind: str, name: str, x: int, y: int = 0, **config: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": kind,
        "name": name,
        "config": config,
        "position": {"x": x, "y": y},
    }


def template_definition(template: str) -> dict[str, Any]:
    """A starting draft. Templates leave the model, knowledge bases and URL empty on
    purpose: the draft shows what is missing, and nothing is guessed for the operator."""
    if template == "rag_answer":
        return {
            "nodes": [
                _node("start", "start", "Question", 0, required_inputs=["question"]),
                _node(
                    "search",
                    "retrieval",
                    "Search knowledge",
                    1,
                    knowledge_base_ids=[],
                    query="{{ input.question }}",
                    top_k=5,
                ),
                _node(
                    "answer",
                    "llm",
                    "Answer",
                    2,
                    model="",
                    system="Answer only from the context. Say so when the context is not enough.",
                    prompt=(
                        "Context:\n{{ nodes.search.context }}\n\nQuestion: {{ input.question }}"
                    ),
                    temperature=0.2,
                ),
                _node(
                    "end",
                    "end",
                    "Done",
                    3,
                    output={
                        "answer": "{{ nodes.answer.text }}",
                        "sources": "{{ nodes.search.passages }}",
                    },
                ),
            ],
            "edges": [
                {"from": "start", "to": "search"},
                {"from": "search", "to": "answer"},
                {"from": "answer", "to": "end"},
            ],
        }
    if template == "approval_http":
        return {
            "nodes": [
                _node("start", "start", "Request", 0),
                _node(
                    "review",
                    "approval",
                    "Review",
                    1,
                    message="Send this request?",
                    details={"input": "{{ input }}"},
                ),
                _node(
                    "call",
                    "http",
                    "Call service",
                    2,
                    method="POST",
                    url="",
                    headers={},
                    body="{{ input }}",
                ),
                _node("done", "end", "Sent", 3, output={"status": "{{ nodes.call.status }}"}),
                _node("declined", "end", "Declined", 2, 1, output={"declined": True}),
            ],
            "edges": [
                {"from": "start", "to": "review"},
                {"from": "review", "to": "call", "when": "approved"},
                {"from": "review", "to": "declined", "when": "rejected"},
                {"from": "call", "to": "done"},
            ],
        }
    return starter_definition()


def permissions_of(user: User) -> list[str]:
    if user.is_superuser:
        return sorted(PERMISSION_CATALOGUE)
    return sorted(user.effective_permissions)


def next_fire(cron: str, after: dt.datetime) -> dt.datetime:
    """The next firing of a five-field UTC cron after ``after``. Raises ValidationError."""
    try:
        trigger = CronTrigger.from_crontab(cron, timezone="UTC")
    except ValueError as exc:
        raise ValidationError(
            f"Not a valid cron expression: {cron!r} ({exc}).",
            details={"field": "schedule_cron"},
        ) from exc
    fire = trigger.get_next_fire_time(None, after)
    if fire is None:
        raise ValidationError("That schedule never fires.", details={"field": "schedule_cron"})
    return fire  # type: ignore[no-any-return]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class WorkflowService:
    def __init__(
        self,
        settings: Settings,
        workflows: WorkflowRepository,
        versions: WorkflowVersionRepository,
        runs: WorkflowRunRepository,
        approvals: WorkflowApprovalRepository,
        users: UserRepository,
        audit: AuditService,
        engine: WorkflowEngine,
    ) -> None:
        self._settings = settings
        self._workflows = workflows
        self._versions = versions
        self._runs = runs
        self._approvals = approvals
        self._users = users
        self._audit = audit
        self._engine = engine

    # ------------------------------------------------------------------ reads
    async def list_workflows(self, *, status: str | None = None) -> list[Workflow]:
        return list(await self._workflows.list_all(status=status))

    async def get(self, workflow_id: uuid.UUID) -> Workflow:
        workflow = await self._workflows.get(workflow_id)
        if workflow is None:
            raise NotFoundError(f"No workflow with id {workflow_id}.")
        return workflow

    async def versions(self, workflow_id: uuid.UUID) -> list[WorkflowVersion]:
        await self.get(workflow_id)
        return list(await self._versions.list_for(workflow_id))

    async def last_runs(self, workflow_ids: list[uuid.UUID]) -> dict[uuid.UUID, WorkflowRun]:
        return await self._runs.last_for(workflow_ids)

    # ------------------------------------------------------------------ edits
    async def create(
        self,
        *,
        name: str,
        display_name: str,
        description: str | None,
        draft: dict[str, Any] | None,
        actor: User,
        team_id: uuid.UUID | None = None,
    ) -> Workflow:
        if await self._workflows.get_by_name(name) is not None:
            raise ConflictError(f"A workflow named {name!r} already exists.")
        workflow = Workflow(
            name=name,
            display_name=display_name,
            description=description,
            status=WorkflowStatus.DRAFT,
            draft=draft or starter_definition(),
            team_id=team_id,
            created_by=actor.id,
        )
        self._workflows.add(workflow)
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": name},
        )
        return workflow

    async def update(
        self, workflow_id: uuid.UUID, changes: dict[str, Any], *, actor: User
    ) -> Workflow:
        """Edit the draft, the description or the schedule. The draft may be invalid while
        it is being built; publishing is what validates."""
        workflow = await self.get(workflow_id)
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("An archived workflow cannot be edited.")
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise ValidationError(f"Cannot change {', '.join(sorted(unknown))}.")
        if "draft" in changes and not isinstance(changes["draft"], dict):
            raise ValidationError("draft must be an object with nodes and edges.")
        if "schedule_cron" in changes:
            cron = (changes["schedule_cron"] or "").strip() or None
            if cron:
                next_fire(cron, dt.datetime.now(dt.UTC))
            changes["schedule_cron"] = cron
        for key, value in changes.items():
            setattr(workflow, key, value)
        self._reschedule(workflow)
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_UPDATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": workflow.name, "changed": sorted(changes)},
        )
        return workflow

    async def publish(self, workflow_id: uuid.UUID, *, note: str | None, actor: User) -> Workflow:
        workflow = await self.get(workflow_id)
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("An archived workflow cannot be published.")
        wd.require_valid(workflow.draft)
        version = (workflow.published_version or 0) + 1
        self._versions.add(
            WorkflowVersion(
                workflow_id=workflow.id,
                version=version,
                definition=workflow.draft,
                note=note,
                published_by=actor.id,
            )
        )
        workflow.published_version = version
        if workflow.status == WorkflowStatus.DRAFT:
            workflow.status = WorkflowStatus.PUBLISHED
        self._reschedule(workflow)
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_PUBLISHED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": workflow.name, "version": version, "note": note},
        )
        return workflow

    async def set_status(self, workflow_id: uuid.UUID, status: str, *, actor: User) -> Workflow:
        workflow = await self.get(workflow_id)
        if status not in (WorkflowStatus.PUBLISHED, WorkflowStatus.PAUSED, WorkflowStatus.ARCHIVED):
            raise ValidationError("status is PUBLISHED, PAUSED or ARCHIVED.")
        if workflow.status == WorkflowStatus.ARCHIVED:
            raise ConflictError("An archived workflow stays archived.")
        if (
            status in (WorkflowStatus.PUBLISHED, WorkflowStatus.PAUSED)
            and not workflow.published_version
        ):
            raise ConflictError("Publish the workflow first.")
        before = workflow.status
        workflow.status = status
        self._reschedule(workflow)
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_STATUS_CHANGED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": workflow.name, "from": before, "to": status},
        )
        return workflow

    async def delete(self, workflow_id: uuid.UUID, *, actor: User) -> None:
        workflow = await self.get(workflow_id)
        counts = await self._runs.count_by_status(workflow.id)
        active = sum(
            n
            for s, n in counts.items()
            if s
            in (
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING,
                RunStatus.WAITING_FOR_APPROVAL,
            )
        )
        if active:
            raise ConflictError(
                f"{active} run(s) are still in progress. Cancel them or archive the workflow."
            )
        await self._workflows.delete(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_DELETED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow_id),
            metadata={"name": workflow.name},
        )

    async def _flush(self, workflow: Workflow) -> None:
        """Flush, then reload ``updated_at``: the database sets it, and an expired column
        would otherwise be loaded lazily, which async sessions cannot do."""
        await self._workflows.flush()
        await self._workflows.session.refresh(workflow, ["updated_at"])

    def _reschedule(self, workflow: Workflow) -> None:
        if workflow.schedule_cron and workflow.status == WorkflowStatus.PUBLISHED:
            workflow.next_run_at = next_fire(workflow.schedule_cron, dt.datetime.now(dt.UTC))
        else:
            workflow.next_run_at = None

    # ------------------------------------------------------------------ webhook
    async def rotate_webhook(self, workflow_id: uuid.UUID, *, actor: User) -> tuple[Workflow, str]:
        """A new webhook token, returned once; the previous one stops working."""
        workflow = await self.get(workflow_id)
        token = "wfh_" + secrets.token_urlsafe(32)
        workflow.webhook_token_hash = hash_token(token)
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_WEBHOOK_ROTATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": workflow.name, "enabled": True},
        )
        return workflow, token

    async def disable_webhook(self, workflow_id: uuid.UUID, *, actor: User) -> Workflow:
        workflow = await self.get(workflow_id)
        workflow.webhook_token_hash = None
        await self._flush(workflow)
        await self._audit.record(
            AuditAction.WORKFLOW_WEBHOOK_ROTATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow",
            resource_id=str(workflow.id),
            metadata={"name": workflow.name, "enabled": False},
        )
        return workflow

    async def verify_webhook(self, workflow_id: uuid.UUID, token: str | None) -> Workflow:
        """The workflow, if ``token`` is its webhook token. One answer for every failure,
        so the endpoint does not reveal which workflows exist or have webhooks."""
        workflow = await self._workflows.get(workflow_id)
        expected = workflow.webhook_token_hash if workflow else None
        presented = hash_token(token or "")
        if workflow is None or not expected or not hmac.compare_digest(expected, presented):
            raise AuthenticationError("Invalid webhook token.")
        return workflow

    # ------------------------------------------------------------------ runs
    async def start(
        self,
        workflow: Workflow,
        *,
        trigger: str,
        input: dict[str, Any] | None,
        actor: User | None,
    ) -> WorkflowRun:
        """Queue a run. The caller decides whether to execute it now (`execute`) or leave
        it to the worker."""
        if trigger not in TRIGGERS:
            raise ValidationError(f"Unknown trigger {trigger!r}.")
        payload = input or {}
        if not isinstance(payload, dict):
            raise ValidationError("A run's input is an object.")
        if len(str(payload)) > MAX_INPUT_BYTES:
            raise ValidationError("The input is too large (64 KiB at most).")

        if trigger == "test":
            wd.require_valid(workflow.draft)
            definition, version = workflow.draft, None
        else:
            if workflow.status != WorkflowStatus.PUBLISHED or not workflow.published_version:
                raise ConflictError(
                    f"The workflow is {workflow.status.lower()}; only a published workflow "
                    "accepts runs. Test runs of the draft are always allowed."
                )
            published = await self._versions.get_version(workflow.id, workflow.published_version)
            if published is None:
                raise ConflictError("The published version is missing; publish the workflow again.")
            definition, version = published.definition, published.version

        if actor is not None:
            permissions = permissions_of(actor)
        else:
            published = await self._versions.get_version(
                workflow.id, workflow.published_version or 0
            )
            publisher = (
                await self._users.get_with_roles(published.published_by)
                if published and published.published_by
                else None
            )
            permissions = permissions_of(publisher) if publisher and publisher.is_active else []

        run = WorkflowRun(
            workflow_id=workflow.id,
            version=version,
            definition=definition,
            trigger=trigger,
            status=RunStatus.QUEUED,
            input=payload,
            state={"nodes": {}},
            user_permissions=permissions,
            started_by=actor.id if actor else None,
        )
        self._runs.add(run)
        await self._runs.flush()
        await self._audit.record(
            AuditAction.WORKFLOW_RUN_STARTED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else f"workflow:{trigger}",
            resource_type="workflow_run",
            resource_id=str(run.id),
            metadata={"workflow": workflow.name, "trigger": trigger, "version": version},
        )
        return run

    async def execute(self, run_id: uuid.UUID) -> WorkflowRun:
        """Advance a run now, under a row lock."""
        run = await self._runs.lock(run_id)
        if run is None:
            raise NotFoundError(f"No workflow run with id {run_id}.")
        return await self._engine.advance(run)

    async def get_run(self, run_id: uuid.UUID) -> WorkflowRun:
        run = await self._runs.get_with_steps(run_id)
        if run is None:
            raise NotFoundError(f"No workflow run with id {run_id}.")
        return run

    async def list_runs(
        self, *, workflow_id: uuid.UUID | None = None, status: str | None = None, limit: int = 50
    ) -> list[WorkflowRun]:
        return list(await self._runs.list_for(workflow_id=workflow_id, status=status, limit=limit))

    async def approvals_for(self, run_id: uuid.UUID) -> list[WorkflowApproval]:
        return list(await self._approvals.list_for_run(run_id))

    async def cancel(self, run_id: uuid.UUID, *, actor: User) -> WorkflowRun:
        run = await self._runs.lock(run_id)
        if run is None:
            raise NotFoundError(f"No workflow run with id {run_id}.")
        if run.is_terminal:
            raise ConflictError(f"The run already {run.status.lower()}.")
        for approval in await self._approvals.pending_for_run(run.id):
            approval.status = ApprovalStatus.EXPIRED
        run.status = RunStatus.CANCELLED
        run.finished_at = dt.datetime.now(dt.UTC)
        run.error = f"Cancelled by {actor.username}."
        await self._runs.flush()
        await self._audit.record(
            AuditAction.WORKFLOW_RUN_CANCELLED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow_run",
            resource_id=str(run.id),
        )
        return run

    # ------------------------------------------------------------------ approvals
    async def pending_approvals(self) -> list[WorkflowApproval]:
        return list(await self._approvals.list_pending())

    async def get_approval(self, approval_id: uuid.UUID) -> WorkflowApproval:
        approval = await self._approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(f"No approval with id {approval_id}.")
        return approval

    async def decide(
        self, approval_id: uuid.UUID, *, approved: bool, reason: str | None, actor: User
    ) -> WorkflowRun:
        approval = await self.get_approval(approval_id)
        run = await self._runs.lock(approval.run_id)
        if run is None:
            raise NotFoundError("The run for this approval no longer exists.")
        await self._approvals.session.refresh(approval)
        if approval.status != ApprovalStatus.PENDING:
            raise ConflictError(f"This step was already {approval.status.lower()}.")
        if run.status != RunStatus.WAITING_FOR_APPROVAL:
            raise ConflictError(f"The run is {run.status.lower()}, not waiting for approval.")
        if (
            not approval.allow_self_approval
            and run.started_by == actor.id
            and not actor.is_superuser
        ):
            raise PermissionDeniedError("You started this run, so someone else has to decide it.")
        await self._audit.record(
            AuditAction.WORKFLOW_APPROVAL_GRANTED
            if approved
            else AuditAction.WORKFLOW_APPROVAL_DENIED,
            user_id=actor.id,
            username=actor.username,
            resource_type="workflow_run",
            resource_id=str(run.id),
            metadata={"node": approval.node_id, "reason": reason, "details": approval.details},
        )
        return await self._engine.decide(
            run, approval, approved=approved, decided_by=actor.id, reason=reason
        )

    # ------------------------------------------------------------------ worker
    async def fire_due_schedule(self, now: dt.datetime) -> WorkflowRun | None:
        """Queue one due scheduled run and move its schedule on. None if nothing is due."""
        workflow = await self._workflows.claim_due_schedule(now)
        if workflow is None:
            return None
        if not workflow.schedule_cron:
            workflow.next_run_at = None
            return None
        # From now, not from the missed time: a platform down for a day fires once on
        # return, not once for every slot it missed.
        workflow.next_run_at = next_fire(workflow.schedule_cron, now)
        try:
            run = await self.start(
                workflow, trigger="schedule", input=dict(workflow.schedule_input or {}), actor=None
            )
        except Exception:
            log.exception("workflow_schedule_failed", workflow=workflow.name)
            await self._workflows.flush()
            return None
        await self._workflows.flush()
        return run

    async def claim_due_run(self, now: dt.datetime) -> WorkflowRun | None:
        run = await self._runs.claim_due(now)
        if run is None:
            return None
        return await self._engine.advance(run)
