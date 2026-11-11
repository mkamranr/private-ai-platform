"""Workflow endpoints (E6): definitions, versions, runs, approvals and the webhook trigger.

Permissions: ``workflow.view`` to read, ``workflow.edit`` to build and publish,
``workflow.execute`` to start runs of a published version, ``workflow.approve`` to decide
paused steps. A run executes with the permissions of whoever started it (frozen on the run),
or, for schedule and webhook runs, of whoever published the version.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Query, Response, status

from app.api.deps import (
    AuthServiceDep,
    EffectiveSettingsDep,
    ScopeDep,
    ScopeGuardDep,
    WorkflowServiceDep,
    require_permission,
)
from app.core.errors import ValidationError
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.models.workflows import Workflow, WorkflowRun
from app.schemas.common import MessageResponse
from app.schemas.workflows import (
    ApprovalRead,
    DecideRequest,
    LastRun,
    NodeType,
    PendingApproval,
    Problem,
    PublishRequest,
    RunDetail,
    RunRead,
    RunRequest,
    StatusRequest,
    ValidateRequest,
    ValidateResponse,
    VersionRead,
    WebhookToken,
    WorkflowCreate,
    WorkflowDetail,
    WorkflowRead,
    WorkflowUpdate,
)
from app.services import workflow_definition as wd
from app.services.workflows import WorkflowService, template_definition

router = APIRouter(tags=["workflows"])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _read(workflow: Workflow, last: WorkflowRun | None = None) -> dict[str, Any]:
    return {
        "id": workflow.id,
        "name": workflow.name,
        "display_name": workflow.display_name,
        "description": workflow.description,
        "team_id": workflow.team_id,
        "status": workflow.status,
        "published_version": workflow.published_version,
        "schedule_cron": workflow.schedule_cron,
        "schedule_input": workflow.schedule_input or {},
        "next_run_at": workflow.next_run_at,
        "webhook_enabled": bool(workflow.webhook_token_hash),
        "node_count": len(wd.nodes_of(workflow.draft or {})),
        "last_run": LastRun(
            id=last.id,
            status=last.status,
            trigger=last.trigger,
            created_at=last.created_at,
            finished_at=last.finished_at,
        )
        if last
        else None,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


async def _detail(service: WorkflowService, workflow: Workflow) -> WorkflowDetail:
    last = (await service.last_runs([workflow.id])).get(workflow.id)
    published = None
    if workflow.published_version:
        for version in await service.versions(workflow.id):
            if version.version == workflow.published_version:
                published = version.definition
                break
    return WorkflowDetail(
        **_read(workflow, last),
        draft=workflow.draft or {},
        unpublished_changes=published is None or published != workflow.draft,
        problems=[Problem(**p.as_dict()) for p in wd.validate(workflow.draft)],
    )


async def _check_references(guard: Any, definition: dict[str, Any]) -> None:
    """Everything a definition points at must be in the caller's scope: a workflow is not a
    way to reach another team's knowledge base or model."""
    if not isinstance(definition, dict):
        return
    await guard.knowledge_base_refs(
        wd.referenced_ids(definition, "retrieval", "knowledge_base_ids")
    )
    for name in wd.referenced_ids(definition, "llm", "model"):
        if "{{" not in name:
            await guard.model_name(name)


async def _run_detail(service: WorkflowService, run_id: uuid.UUID) -> RunDetail:
    run = await service.get_run(run_id)
    approvals = await service.approvals_for(run_id)
    detail = RunDetail.model_validate(run)
    detail.steps = sorted(detail.steps, key=lambda s: s.sequence)
    detail.approvals = [ApprovalRead.model_validate(a) for a in approvals]
    return detail


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------
@router.get("/workflows/node-types", response_model=list[NodeType], summary="Workflow node types")
async def list_node_types(
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
) -> list[NodeType]:
    return [
        NodeType(type=kind, description=text, branches=sorted(wd.BRANCHES.get(kind, ())))
        for kind, text in wd.NODE_TYPES.items()
    ]


@router.get("/workflows", response_model=list[WorkflowRead], summary="List workflows")
async def list_workflows(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    scope: ScopeDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[WorkflowRead]:
    workflows = scope.filter(await service.list_workflows(status=status_filter))
    last = await service.last_runs([w.id for w in workflows])
    return [WorkflowRead(**_read(w, last.get(w.id))) for w in workflows]


@router.post(
    "/workflows",
    response_model=WorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow",
)
async def create_workflow(
    payload: WorkflowCreate,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
) -> WorkflowDetail:
    """Create a draft. It can be saved while incomplete; publishing is what validates."""
    await guard.assignable(payload.team_id)
    draft = payload.draft if payload.draft is not None else template_definition(payload.template)
    await _check_references(guard, draft)
    workflow = await service.create(
        name=payload.name,
        display_name=payload.display_name,
        description=payload.description,
        draft=draft,
        actor=actor,
        # Checked by guard.assignable above; set before the first flush.
        team_id=payload.team_id,
    )
    return await _detail(service, workflow)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetail, summary="Get a workflow")
async def get_workflow(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> WorkflowDetail:
    await guard.workflow(workflow_id)
    return await _detail(service, await service.get(workflow_id))


@router.patch(
    "/workflows/{workflow_id}", response_model=WorkflowDetail, summary="Update a workflow"
)
async def update_workflow(
    payload: WorkflowUpdate,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> WorkflowDetail:
    """Change the draft, the description or the schedule. A schedule takes effect for the
    published version only, and fires in UTC."""
    await guard.workflow(workflow_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("draft") is not None:
        await _check_references(guard, changes["draft"])
    workflow = await service.update(workflow_id, changes, actor=actor)
    return await _detail(service, workflow)


@router.delete(
    "/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a workflow"
)
async def delete_workflow(
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> Response:
    """Delete a workflow with its versions and run history. Refused while runs are in
    progress; archive it instead to keep the history."""
    await guard.workflow(workflow_id)
    await service.delete(workflow_id, actor=actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/workflows/{workflow_id}/validate",
    response_model=ValidateResponse,
    summary="Validate a definition",
)
async def validate_workflow(
    payload: ValidateRequest,
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> ValidateResponse:
    await guard.workflow(workflow_id)
    definition = payload.definition
    if definition is None:
        definition = (await service.get(workflow_id)).draft
    problems = wd.validate(definition)
    order = wd.execution_order(definition) if not problems else []
    return ValidateResponse(
        valid=not problems, problems=[Problem(**p.as_dict()) for p in problems], order=order
    )


@router.post(
    "/workflows/{workflow_id}/publish",
    response_model=WorkflowDetail,
    summary="Publish the draft as a new version",
)
async def publish_workflow(
    payload: PublishRequest,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> WorkflowDetail:
    """Validate the draft and freeze it as the next version. Runs already in progress keep
    the version they started with."""
    await guard.workflow(workflow_id)
    workflow = await service.get(workflow_id)
    await _check_references(guard, workflow.draft)
    workflow = await service.publish(workflow_id, note=payload.note, actor=actor)
    return await _detail(service, workflow)


@router.post(
    "/workflows/{workflow_id}/status",
    response_model=WorkflowDetail,
    summary="Pause, resume or archive",
)
async def set_workflow_status(
    payload: StatusRequest,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> WorkflowDetail:
    """PAUSED stops new runs (manual, schedule and webhook); ARCHIVED is final."""
    await guard.workflow(workflow_id)
    workflow = await service.set_status(workflow_id, payload.status, actor=actor)
    return await _detail(service, workflow)


@router.get(
    "/workflows/{workflow_id}/versions",
    response_model=list[VersionRead],
    summary="Published versions",
)
async def list_versions(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> list[VersionRead]:
    await guard.workflow(workflow_id)
    return [VersionRead.model_validate(v) for v in await service.versions(workflow_id)]


@router.post(
    "/workflows/{workflow_id}/webhook",
    response_model=WebhookToken,
    summary="Enable the webhook or rotate its token",
)
async def rotate_webhook(
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    settings: EffectiveSettingsDep,
    workflow_id: uuid.UUID = Path(...),
) -> WebhookToken:
    """A new token, shown once. The previous token stops working at once."""
    await guard.workflow(workflow_id)
    workflow, token = await service.rotate_webhook(workflow_id, actor=actor)
    base = settings.platform.public_base_url.rstrip("/")
    return WebhookToken(
        token=token,
        url=f"{base}/api/v1/workflow-hooks/{workflow.id}",
        message="Send it in the X-Workflow-Token header. It will not be shown again.",
    )


@router.delete(
    "/workflows/{workflow_id}/webhook",
    response_model=MessageResponse,
    summary="Disable the webhook",
)
async def disable_webhook(
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EDIT)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> MessageResponse:
    await guard.workflow(workflow_id)
    await service.disable_webhook(workflow_id, actor=actor)
    return MessageResponse(message="Webhook disabled.")


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Start a run",
)
async def start_run(
    payload: RunRequest,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EXECUTE)],
    auth: AuthServiceDep,
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
) -> RunDetail:
    """Start a run of the published version, or with `test` of the saved draft (which
    needs `workflow.edit` as well).

    With `wait` (the default) the run executes in this request and the response shows where
    it ended: finished, failed, or paused for an approval or a delay. Without it the run is
    queued and the worker picks it up within seconds.
    """
    await guard.workflow(workflow_id)
    if payload.test:
        await auth.require_permission(actor, Perm.WORKFLOW_EDIT)
    workflow = await service.get(workflow_id)
    run = await service.start(
        workflow,
        trigger="test" if payload.test else payload.trigger,
        input=payload.input,
        actor=actor,
    )
    if payload.wait:
        await service.execute(run.id)
    return await _run_detail(service, run.id)


@router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[RunRead],
    summary="Runs of a workflow",
)
async def list_workflow_runs(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    workflow_id: uuid.UUID = Path(...),
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunRead]:
    await guard.workflow(workflow_id)
    runs = await service.list_runs(workflow_id=workflow_id, status=status_filter, limit=limit)
    return [RunRead.model_validate(r) for r in runs]


@router.get("/workflow-runs", response_model=list[RunRead], summary="Recent workflow runs")
async def list_runs(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RunRead]:
    runs = await service.list_runs(status=status_filter, limit=limit)
    visible = await guard.visible_workflows({r.workflow_id for r in runs})
    return [RunRead.model_validate(r) for r in runs if r.workflow_id in visible]


@router.get(
    "/workflow-runs/{run_id}", response_model=RunDetail, summary="A run with its step trace"
)
async def get_run(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_VIEW)],
    guard: ScopeGuardDep,
    run_id: uuid.UUID = Path(...),
) -> RunDetail:
    await guard.workflow_run(run_id)
    return await _run_detail(service, run_id)


@router.post("/workflow-runs/{run_id}/cancel", response_model=RunDetail, summary="Cancel a run")
async def cancel_run(
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_EXECUTE)],
    guard: ScopeGuardDep,
    run_id: uuid.UUID = Path(...),
) -> RunDetail:
    """Stop a run that is queued, paused or waiting. Pending approvals on it expire."""
    await guard.workflow_run(run_id)
    await service.cancel(run_id, actor=actor)
    return await _run_detail(service, run_id)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
@router.get(
    "/workflow-approvals/pending",
    response_model=list[PendingApproval],
    summary="Workflow steps awaiting approval",
)
async def list_pending_approvals(
    service: WorkflowServiceDep,
    _actor: Annotated[User, require_permission(Perm.WORKFLOW_APPROVE)],
    guard: ScopeGuardDep,
) -> list[PendingApproval]:
    pending = await service.pending_approvals()
    visible = await guard.visible_workflows({a.run.workflow_id for a in pending})
    return [
        PendingApproval(
            **ApprovalRead.model_validate(a).model_dump(),
            workflow_id=a.run.workflow_id,
            workflow_name=a.run.workflow.name,
            workflow_display_name=a.run.workflow.display_name,
            started_by=a.run.started_by,
            trigger=a.run.trigger,
        )
        for a in pending
        if a.run.workflow_id in visible
    ]


@router.post(
    "/workflow-approvals/{approval_id}/decide",
    response_model=RunDetail,
    summary="Approve or reject a paused step",
)
async def decide_approval(
    payload: DecideRequest,
    service: WorkflowServiceDep,
    actor: Annotated[User, require_permission(Perm.WORKFLOW_APPROVE)],
    guard: ScopeGuardDep,
    approval_id: uuid.UUID = Path(...),
) -> RunDetail:
    """Record the decision and continue the run in this request. Whoever started a run
    cannot decide its approvals unless the step allows it (a superuser always can)."""
    await guard.workflow_approval(approval_id)
    run = await service.decide(
        approval_id, approved=payload.approved, reason=payload.reason, actor=actor
    )
    return await _run_detail(service, run.id)


# ---------------------------------------------------------------------------
# Webhook trigger (public; the token is the credential)
# ---------------------------------------------------------------------------
@router.post(
    "/workflow-hooks/{workflow_id}",
    response_model=RunRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a workflow by webhook",
)
async def webhook(
    service: WorkflowServiceDep,
    body: dict[str, Any] | None = None,
    workflow_id: uuid.UUID = Path(...),
    token: Annotated[str | None, Header(alias="X-Workflow-Token")] = None,
) -> RunRead:
    """Queue a run of the published version with the JSON body as its input. Authenticated
    by the workflow's webhook token, not a user: the run executes with the permissions of
    whoever published the version. Always 202 — follow the run through the API."""
    workflow = await service.verify_webhook(workflow_id, token)
    if body is not None and not isinstance(body, dict):
        raise ValidationError("The webhook body is a JSON object.")
    run = await service.start(workflow, trigger="webhook", input=body or {}, actor=None)
    return RunRead.model_validate(run)
