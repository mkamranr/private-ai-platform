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
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.errors import AppError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit import AuditAction
from app.models.auth import User
from app.models.models_registry import (
    DeploymentRollout,
    DeploymentState,
    RolloutState,
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
        self, model_id: uuid.UUID, change: RevisionChange, *, actor: User
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
            metadata={"rollout_id": str(rollout.id), "changes": target, "copies": len(copies)},
        )
        return rollout

    async def cancel(self, model_id: uuid.UUID, *, actor: User) -> DeploymentRollout:
        """Stop before the next copy. A copy being revised finishes its own revision."""
        rollout = await self._rollouts.running_for_model(model_id)
        if rollout is None:
            raise NotFoundError("No rollout of this model is running.")
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
