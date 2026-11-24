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

**More than one canary copy, and latency (E82).** ``copies`` (default 1) copies are
revised before the watch starts, each on ``percent`` as soon as it is healthy, so a fault
that shows only under some load, or on one node, has more calls to show up in; at least
one copy keeps the old configuration. With ``max_latency_ratio``, the canary copies' p95
latency over the watch is compared with the old copies' over the same minutes, on calls
that did not fail: more than that many times slower stops the rollout as errors do. Too
few calls on either side (``LATENCY_MIN_CALLS``) and latency isn't judged; the result
says so.
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

#: Fewer successful calls than this on the canary or the old copies: latency not judged.
LATENCY_MIN_CALLS = 10

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
        if canary and int(canary.get("copies") or 1) >= len(copies):
            raise ValidationError(
                f"This model runs {len(copies)} copies, so at most {len(copies) - 1} can be "
                "canaries: at least one keeps the old configuration while they are watched.",
                details={"field": "canary_copies"},
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
        if canary and canary.get("state") in (None, "watching"):
            # The copies under watch (or given their share already) go back to full.
            for ident in _canary_ids(canary):
                await self._set_weight(ident, 100)
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
            wanted = int(canary.get("copies") or 1)
            # Each revised copy takes the canary's share as soon as it is healthy, so
            # the first doesn't serve at full share while the next one is revised.
            revised = [i for i in rollout.deployment_ids if rollout.progress.get(i)][:wanted]
            for ident in revised:
                if ident not in _canary_ids(canary):
                    await self._set_weight(ident, int(canary["percent"]))
            canary["deployment_ids"] = revised
            if len(revised) < wanted:
                rollout.canary = canary
                await self._rollouts.flush()
                return None
            rollout.canary = {
                **canary,
                "state": "watching",
                "deployment_id": revised[0],
                "started_at": now.isoformat(),
                "until": (now + dt.timedelta(minutes=int(canary["minutes"]))).isoformat(),
            }
            await self._rollouts.flush()
            log.info("rollout_canary_started", rollout=str(rollout.id), deployments=revised)
            return False
        if now < dt.datetime.fromisoformat(canary["until"]):
            return False
        watched = _canary_ids(canary)
        since = dt.datetime.fromisoformat(canary["started_at"])
        calls, errors = await self._calls(watched, since)
        share = errors * 100 / calls if calls else 0.0
        limit = float(canary.get("max_error_percent") or 0)
        results: dict[str, Any] = {**canary, "calls": calls, "errors": errors}
        names = ", ".join(i[:8] for i in watched)
        label = f"Canary cop{'ies' if len(watched) > 1 else 'y'} {names}"
        failure = None
        if calls and share > limit:
            failure = f"{errors} of {calls} calls failed ({share:.1f}%, limit {limit:g}%)"
        ratio_limit = canary.get("max_latency_ratio")
        if ratio_limit:
            others = [i for i in rollout.deployment_ids if i not in watched]
            mine, mine_n = await self._p95(watched, since)
            theirs, theirs_n = await self._p95(others, since)
            results.update(latency_p95_ms=mine, baseline_p95_ms=theirs)
            if min(mine_n, theirs_n) < LATENCY_MIN_CALLS or not theirs:
                results["latency_note"] = (
                    f"Latency not judged: {mine_n} successful calls on the canary and "
                    f"{theirs_n} on the other copies (needs {LATENCY_MIN_CALLS} each)."
                )
            elif mine is not None and mine > theirs * float(ratio_limit) and failure is None:
                failure = (
                    f"p95 latency {mine:.0f} ms against {theirs:.0f} ms on the other copies "
                    f"({mine / theirs:.2f}x, limit {float(ratio_limit):g}x)"
                )
        if failure:
            for ident in watched:
                await self._set_weight(ident, 0)
            rollout.canary = {**results, "state": "failed"}
            await self._finish(
                rollout,
                RolloutState.FAILED,
                f"{label}: {failure}. They take no new calls now; roll them back from their "
                "revisions, or give them their share again.",
            )
            return True
        for ident in watched:
            await self._set_weight(ident, 100)
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

    async def _calls(self, idents: list[str], since: dt.datetime) -> tuple[int, int]:
        """Calls served by the copies since ``since``, and how many failed (5xx)."""
        stmt = select(
            func.count(UsageRecord.id),
            func.coalesce(func.sum(case((UsageRecord.status_code >= 500, 1), else_=0)), 0),
        ).where(
            UsageRecord.deployment_id.in_([uuid.UUID(i) for i in idents]),
            UsageRecord.recorded_at >= since,
        )
        total, failed = (await self._rollouts.session.execute(stmt)).one()
        return int(total or 0), int(failed or 0)

    async def _p95(self, idents: list[str], since: dt.datetime) -> tuple[float | None, int]:
        """E82: p95 latency of the copies' successful calls since ``since``, and how many."""
        if not idents:
            return None, 0
        stmt = select(
            func.percentile_cont(0.95).within_group(UsageRecord.latency_ms),
            func.count(UsageRecord.id),
        ).where(
            UsageRecord.deployment_id.in_([uuid.UUID(i) for i in idents]),
            UsageRecord.recorded_at >= since,
            UsageRecord.status_code < 400,
        )
        p95, count = (await self._rollouts.session.execute(stmt)).one()
        return (float(p95) if p95 is not None else None), int(count or 0)

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


def _canary_ids(canary: dict[str, Any]) -> list[str]:
    """The canary copies: E82 keeps a list; an E77 rollout kept one ``deployment_id``."""
    ids = canary.get("deployment_ids")
    if ids:
        return [str(i) for i in ids]
    return [str(canary["deployment_id"])] if canary.get("deployment_id") else []
