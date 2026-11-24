"""A configuration change rolled out to every copy of a model, one at a time (E61).

A revision changes one deployment. When a model runs as several copies (E58 spreads its
calls across them), changing all of them by hand means watching each come back before
touching the next, or taking the model down. A rollout does that watching:

    start  ->  revise copy 1  ->  wait until healthy  ->  revise copy 2  ->  ...  ->  done

Each copy is revised with the ``auto`` strategy, so while one restarts the others serve;
a model with a single copy gets a new copy that takes over once healthy (blue/green).

**A copy that fails stops the rollout.** The copies not yet reached keep their old
configuration and keep serving; the failed one can be rolled back from its revisions.
Rolling on regardless would take the whole model down for a bad image.

**Driven by the deployment worker**, one step per pass, with the rollout's state in the
database, so a control-plane restart resumes it.

**A canary first (E77).** With ``canary``, the first copy, once revised and healthy, gets
``percent`` of the model's new calls (its ``traffic_weight``) for ``minutes``. Then its
calls in that time are counted from the usage records: if more than
``max_error_percent`` of them failed (a 5xx), the rollout stops there and the copy is set
to take no new calls (weight 0) until someone rolls it back or raises it again;
otherwise its weight goes back to 100 and the other copies follow. It needs at least two
copies, so the old configuration keeps serving the rest.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import case, func, select

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import (
    DeploymentRollout,
    DeploymentState,
    RolloutState,
    UsageRecord,
)
from app.repositories.models_registry import DeploymentRolloutRepository
from app.repositories.user import UserRepository
from app.services.audit import AuditService
from app.services.deployment import (
    WORKER_ACTOR,
    DeploymentService,
    RevisionChange,
    change_target,
)

log = get_logger(__name__)

#: States in which a copy is still on its way to RUNNING or FAILED.
_SETTLING = frozenset(
    {
        DeploymentState.REQUESTED,
        DeploymentState.VALIDATING,
        DeploymentState.SCHEDULING,
        DeploymentState.CREATING,
        DeploymentState.STARTING,
        DeploymentState.HEALTH_CHECK,
        DeploymentState.STOPPING,
    }
)


class RolloutService:
    def __init__(
        self,
        deployments: DeploymentService,
        repository: DeploymentRolloutRepository,
        audit: AuditService,
    ) -> None:
        self._deployments = deployments
        self._rollouts = repository
        self._audit = audit
        self._users = UserRepository(repository.session)

    async def latest(self, model_id: uuid.UUID) -> DeploymentRollout | None:
        return await self._rollouts.latest_for_model(model_id)

    async def start(
        self,
        model_id: uuid.UUID,
        change: RevisionChange,
        *,
        actor: User,
        canary: dict[str, Any] | None = None,
    ) -> DeploymentRollout:
        if await self._rollouts.running_for_model(model_id):
            raise ConflictError("A rollout of this model is already running.")
        target = change_target(change)
        if not target:
            raise ValidationError("Nothing to change: give at least one setting.")
        copies = [
            d
            for d in await self._deployments.list_deployments(model_id=model_id)
            if d.node_id is not None
            and d.replaces_id is None
            and d.state == DeploymentState.RUNNING
        ]
        if not copies:
            raise ValidationError(
                "This model has no running copy the platform manages, so there is nothing to "
                "roll out to."
            )
        # Every copy checked first: a rollout that fails half-way on a setting one copy
        # cannot take leaves the model running two configurations.
        problems = []
        for copy in copies:
            try:
                await self._deployments.check_revision(copy, target)
            except AppError as exc:
                problems.append(f"{str(copy.id)[:8]}: {exc.message}")
        if problems:
            raise ValidationError(
                "The change cannot be applied to every copy: " + "; ".join(problems),
                details={"copies": problems},
            )
        if canary and len(copies) < 2:
            raise ValidationError(
                "A canary needs at least two running copies, so the old configuration keeps "
                "serving while the first is watched. Revise a single copy with the auto "
                "strategy instead.",
                details={"field": "canary_percent"},
            )
        copies.sort(key=lambda d: (d.created_at, str(d.id)))
        rollout = DeploymentRollout(
            model_id=model_id,
            state=RolloutState.RUNNING,
            change=target,
            change_note=change.change_note,
            deployment_ids=[str(d.id) for d in copies],
            progress={},
            done_count=0,
            created_by=actor.id,
            canary=dict(canary) if canary else None,
        )
        self._rollouts.add(rollout)
        await self._rollouts.flush()
        await self._audit.record(
            AuditAction.DEPLOYMENT_ROLLOUT_STARTED,
            user_id=actor.id,
            username=actor.username,
            resource_type="model",
            resource_id=str(model_id),
            message=change.change_note,
            metadata={
                "rollout_id": str(rollout.id),
                "changes": target,
                "copies": len(copies),
                "canary": canary,
            },
        )
        return rollout

    async def cancel(self, model_id: uuid.UUID, *, actor: User) -> DeploymentRollout:
        """Stop before the next copy. A copy being revised finishes its own revision."""
        rollout = await self._rollouts.running_for_model(model_id)
        if rollout is None:
            raise NotFoundError("No rollout of this model is running.")
        canary = rollout.canary or {}
        if canary.get("state") == "watching":
            # The copy under watch is healthy; it goes back to its full share.
            await self._set_weight(canary.get("deployment_id"), 100)
            rollout.canary = {**canary, "state": "cancelled"}
        await self._finish(rollout, RolloutState.CANCELLED, None, actor=actor)
        return rollout

    # -- worker ----------------------------------------------------------------
    async def step(self) -> int:
        """Advance every running rollout by at most one copy. The caller commits."""
        moved = 0
        for rollout in await self._rollouts.running():
            try:
                if await self._advance(rollout):
                    moved += 1
            except Exception as exc:
                log.exception("rollout_step_failed", rollout=str(rollout.id))
                await self._finish(
                    rollout, RolloutState.FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"
                )
        return moved

    async def _advance(self, rollout: DeploymentRollout) -> bool:
        if rollout.current_deployment_id is not None:
            settled = await self._settle(rollout)
            if not settled:
                return False
        if rollout.state != RolloutState.RUNNING:
            return True
        held = await self._canary(rollout)
        if held is not None:
            return held
        remaining = [i for i in rollout.deployment_ids if i not in rollout.progress]
        while remaining:
            ident = remaining.pop(0)
            copy = await self._deployments.find_deployment(uuid.UUID(ident))
            if copy is None or copy.state != DeploymentState.RUNNING:
                # Deleted or stopped since the rollout began: nothing to roll out to.
                rollout.progress = {**rollout.progress, ident: None}
                continue
            actor = await self._users.get(rollout.created_by) if rollout.created_by else None
            try:
                outcome = await self._deployments.revise_deployment(
                    copy.id,
                    RevisionChange(**rollout.change, change_note=self._note(rollout)),
                    actor=actor,
                )
            except AppError as exc:
                await self._finish(rollout, RolloutState.FAILED, f"Copy {ident[:8]}: {exc.message}")
                return True
            rollout.progress = {**rollout.progress, ident: outcome.revision.revision}
            rollout.current_deployment_id = copy.id
            await self._rollouts.flush()
            log.info(
                "rollout_copy_revised",
                rollout=str(rollout.id),
                deployment=ident,
                strategy=outcome.strategy,
            )
            return True
        await self._finish(rollout, RolloutState.DONE, None)
        return True

    async def _settle(self, rollout: DeploymentRollout) -> bool:
        """Whether the copy being revised is done. Fails the rollout if it failed."""
        ident = str(rollout.current_deployment_id)
        wanted = rollout.progress.get(ident)
        copy = await self._deployments.find_deployment(uuid.UUID(ident))
        if copy is None:
            rollout.current_deployment_id = None
            return True
        if copy.state in _SETTLING or await self._deployments.has_successor(copy.id):
            return False
        if copy.state == DeploymentState.RUNNING and copy.current_revision == wanted:
            rollout.done_count += 1
            rollout.current_deployment_id = None
            await self._rollouts.flush()
            return True
        detail = copy.error_message or copy.state_detail or copy.state
        await self._finish(
            rollout,
            RolloutState.FAILED,
            f"Copy {ident[:8]} did not come back with revision {wanted}: {detail}"[:1000],
        )
        return False

    async def _canary(self, rollout: DeploymentRollout) -> bool | None:
        """E77: None to carry on with the next copy; otherwise the canary holds the
        rollout (False) or has just stopped it (True)."""
        canary = dict(rollout.canary or {})
        if not canary or canary.get("state") in ("passed", "failed", "cancelled"):
            return None
        now = dt.datetime.now(dt.UTC)
        if canary.get("state") is None:
            if rollout.done_count < 1:
                return None
            first = next((i for i in rollout.deployment_ids if rollout.progress.get(i)), None)
            if first is None:
                return None
            await self._set_weight(first, int(canary["percent"]))
            rollout.canary = {
                **canary,
                "state": "watching",
                "deployment_id": first,
                "started_at": now.isoformat(),
                "until": (now + dt.timedelta(minutes=int(canary["minutes"]))).isoformat(),
            }
            await self._rollouts.flush()
            log.info("rollout_canary_started", rollout=str(rollout.id), deployment=first)
            return False
        if now < dt.datetime.fromisoformat(canary["until"]):
            return False
        calls, errors = await self._calls(
            canary["deployment_id"], dt.datetime.fromisoformat(canary["started_at"])
        )
        share = errors * 100 / calls if calls else 0.0
        limit = float(canary.get("max_error_percent") or 0)
        results = {**canary, "calls": calls, "errors": errors}
        if calls and share > limit:
            await self._set_weight(canary["deployment_id"], 0)
            rollout.canary = {**results, "state": "failed"}
            await self._finish(
                rollout,
                RolloutState.FAILED,
                f"Canary copy {str(canary['deployment_id'])[:8]}: {errors} of {calls} calls "
                f"failed ({share:.1f}%, limit {limit:g}%). It takes no new calls now; roll it "
                "back from its revisions, or give it its share again.",
            )
            return True
        await self._set_weight(canary["deployment_id"], 100)
        rollout.canary = {**results, "state": "passed"}
        await self._rollouts.flush()
        log.info("rollout_canary_passed", rollout=str(rollout.id), calls=calls, errors=errors)
        return None

    async def _set_weight(self, ident: Any, weight: int) -> None:
        if not ident:
            return
        copy = await self._deployments.find_deployment(uuid.UUID(str(ident)))
        if copy is not None:
            copy.traffic_weight = weight
            await self._rollouts.flush()

    async def _calls(self, ident: Any, since: dt.datetime) -> tuple[int, int]:
        """Calls served by the copy since ``since``, and how many failed (5xx)."""
        stmt = select(
            func.count(UsageRecord.id),
            func.coalesce(func.sum(case((UsageRecord.status_code >= 500, 1), else_=0)), 0),
        ).where(
            UsageRecord.deployment_id == uuid.UUID(str(ident)),
            UsageRecord.recorded_at >= since,
        )
        total, failed = (await self._rollouts.session.execute(stmt)).one()
        return int(total or 0), int(failed or 0)

    async def _finish(
        self,
        rollout: DeploymentRollout,
        state: str,
        error: str | None,
        *,
        actor: User | None = None,
    ) -> None:
        rollout.state = state
        rollout.error = error
        rollout.finished_at = dt.datetime.now(dt.UTC)
        rollout.current_deployment_id = None
        await self._rollouts.flush()
        await self._audit.record(
            AuditAction.DEPLOYMENT_ROLLOUT_FINISHED,
            user_id=actor.id if actor else None,
            username=actor.username if actor else WORKER_ACTOR,
            resource_type="model",
            resource_id=str(rollout.model_id),
            metadata={
                "rollout_id": str(rollout.id),
                "outcome": str(state).lower(),
                "copies_done": rollout.done_count,
                "error": error,
            },
        )
        log.info("rollout_finished", rollout=str(rollout.id), state=str(state))

    @staticmethod
    def _note(rollout: DeploymentRollout) -> str:
        note = f"Rollout {str(rollout.id)[:8]}"
        return f"{note}: {rollout.change_note}" if rollout.change_note else note
