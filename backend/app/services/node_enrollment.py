"""Node self-enrolment (M04).

An administrator adds a node by **name**. The platform mints a one-time token and hands
back a command. The operator runs it on the GPU host from the offline bundle; the script
installs the agent, generates the agent's own token locally, and calls back here. This
service verifies the token, probes the host to confirm it is really there and really is
who it claims to be, and only then creates the node.

**The order of operations is the whole design.** It is tempting to claim the token first
and sort the rest out afterwards, and that is wrong in both directions: it either holds a
row lock across an outbound HTTP call, or it leaves a token spent with no node behind it
when the probe fails. Instead:

    look up (no lock) -> validate the address -> probe back -> check the name
                      -> claim + create, in one transaction

So an unknown token never causes an outbound request, a failed probe never creates state
and never burns the invitation, and there is no window where the token is gone but the
node is missing.

The consequence to remember when editing: steps 2-4 fail by raising, which rolls the
request transaction back. Anything that must survive a failure — the attempt counter, the
last error — is therefore written on an **independent session**, the same way
:meth:`AuditService.record_independent` does it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config.settings import Settings
from app.core.agent_url import AgentUrlError, validate_agent_url
from app.core.errors import ConflictError, NotFoundError, TokenError, ValidationError
from app.core.logging import get_logger
from app.core.security import generate_enrollment_token, hash_api_key
from app.models.audit import AuditAction, AuditResult
from app.models.auth import User
from app.models.infrastructure import EnrollmentStatus, Node, NodeEnrollment
from app.repositories.infrastructure import NodeEnrollmentRepository, NodeRepository
from app.services.audit import AuditService
from app.services.infrastructure import NodeService, SyncResult

log = get_logger(__name__)

# One message for every rejected token. Whether it was unknown, expired, already used or
# revoked is recorded on the row and in the audit log, where an administrator can see it —
# but telling the *caller* which would let anyone holding a guess learn how close they are.
_REJECTED = "That enrolment token is not valid. Ask an administrator to issue a new one."


class NodeEnrollmentService:
    def __init__(
        self,
        settings: Settings,
        enrollments: NodeEnrollmentRepository,
        nodes: NodeRepository,
        node_service: NodeService,
        audit: AuditService,
        session_factory: async_sessionmaker[Any] | None = None,
    ) -> None:
        self._settings = settings
        self._config = settings.enrollment
        self._enrollments = enrollments
        self._nodes = nodes
        self._nodes_service = node_service
        self._audit = audit
        self._session_factory = session_factory

    # -- administrator side ------------------------------------------------
    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        labels: dict[str, Any] | None = None,
        verify_tls: bool = True,
        ttl_seconds: int | None = None,
        reenroll: bool = False,
        actor: User,
    ) -> tuple[NodeEnrollment, str]:
        """Mint an invitation. Returns the row and the plaintext token, shown once."""
        # Before the uniqueness checks, or an elapsed invitation nobody swept would block
        # this name for ever — the partial unique index only constrains PENDING rows.
        await self._enrollments.expire_stale()

        existing = await self._nodes.get_by_name(name)
        if existing and not reenroll:
            raise ConflictError(
                f"A node named {name!r} is already registered. Re-enrol it to replace its "
                "agent token, or remove it first.",
                details={"field": "name"},
            )
        if await self._enrollments.pending_for_name(name):
            raise ConflictError(
                f"An enrolment for {name!r} is already open. Revoke it before issuing another.",
                details={"field": "name"},
            )

        ttl = min(ttl_seconds or self._config.token_ttl_seconds, 86400)
        token, prefix, token_hash = generate_enrollment_token()
        enrollment = NodeEnrollment(
            node_name=name,
            description=description,
            labels=labels or {},
            verify_tls=verify_tls,
            node_id=existing.id if existing else None,
            token_prefix=prefix,
            token_hash=token_hash,
            status=EnrollmentStatus.PENDING,
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl),
            created_by=actor.id,
        )
        self._enrollments.add(enrollment)
        await self._enrollments.flush()

        await self._audit.record(
            AuditAction.NODE_ENROLLMENT_CREATED,
            user_id=actor.id,
            username=actor.username,
            resource_type="node_enrollment",
            resource_id=str(enrollment.id),
            # The prefix, never the token. `_REDACT_KEYS` would not save us here — it
            # matches exact key names, so a stray `{"token": ...}` is caught but a
            # `{"enrolment": ...}` would not be.
            metadata={"name": name, "prefix": prefix, "reenroll": bool(existing)},
        )
        return enrollment, token

    async def list(
        self, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[list[NodeEnrollment], int]:
        await self._enrollments.expire_stale()
        rows = await self._enrollments.list_filtered(status=status, limit=limit, offset=offset)
        return list(rows), await self._enrollments.count_filtered(status=status)

    async def get(self, enrollment_id: uuid.UUID) -> NodeEnrollment:
        enrollment = await self._enrollments.get(enrollment_id)
        if enrollment is None:
            raise NotFoundError(f"No enrolment with id {enrollment_id}.")
        return enrollment

    async def revoke(self, enrollment_id: uuid.UUID, *, actor: User) -> NodeEnrollment:
        enrollment = await self.get(enrollment_id)
        if enrollment.status == EnrollmentStatus.CONSUMED:
            # Not 200: returning success would imply the node had been un-enrolled, which
            # it has not. The node is real and still holds a working token.
            raise ConflictError(
                f"That enrolment already produced node {enrollment.node_name!r}. Remove "
                "the node instead.",
            )
        if enrollment.status != EnrollmentStatus.REVOKED:
            enrollment.status = EnrollmentStatus.REVOKED
            enrollment.revoked_at = dt.datetime.now(dt.UTC)
            enrollment.revoked_by = actor.id
            await self._audit.record(
                AuditAction.NODE_ENROLLMENT_REVOKED,
                user_id=actor.id,
                username=actor.username,
                resource_type="node_enrollment",
                resource_id=str(enrollment.id),
                metadata={"name": enrollment.node_name, "prefix": enrollment.token_prefix},
            )
        return enrollment

    # -- node side ---------------------------------------------------------
    async def resolve_token(self, presented: str) -> NodeEnrollment:
        """Find a live invitation for a presented token, or refuse.

        Deliberately does no I/O beyond the indexed lookup: an unknown token must not be
        able to make this control plane do anything at all.
        """
        if not presented:
            raise TokenError(_REJECTED)
        enrollment = await self._enrollments.get_by_token_hash(hash_api_key(presented))
        if enrollment is None:
            # 401, not 422. A credential that does not identify anything is an
            # authentication failure, and a platform JWT presented here lands exactly
            # there — which is the behaviour the tests pin.
            await self._record_denied(None, "unknown token")
            raise TokenError(_REJECTED)

        now = dt.datetime.now(dt.UTC)
        if enrollment.status == EnrollmentStatus.CONSUMED:
            # A distinct signal, not ordinary noise: a run of these against one prefix is
            # somebody replaying a script they should not have.
            await self._record_denied(enrollment, "token already used")
            raise TokenError(_REJECTED)
        if enrollment.status != EnrollmentStatus.PENDING or enrollment.expires_at <= now:
            await self._record_denied(enrollment, f"token {enrollment.status.lower()}")
            raise TokenError(_REJECTED)
        if enrollment.attempts >= self._config.max_attempts_per_token:
            await self._record_denied(enrollment, "too many attempts")
            raise TokenError(_REJECTED)
        return enrollment

    async def consume(
        self,
        enrollment: NodeEnrollment,
        *,
        agent_token: str,
        advertised_url: str | None,
        reported_name: str | None,
        source_ip: str | None,
    ) -> tuple[Node, SyncResult]:
        """Verify the host, then create or update the node. See the module docstring."""
        if agent_token == "" or len(agent_token) < 32:
            raise ValidationError(
                "The agent token must be at least 32 characters.",
                details={"field": "agent_token"},
            )

        # Validate before probing. A rejected address must cost no outbound request.
        candidate = advertised_url or self._from_source(source_ip)
        try:
            target = validate_agent_url(candidate, self._config)
        except AgentUrlError as exc:
            await self._fail(enrollment, str(exc), candidate)
            raise ValidationError(str(exc), details={"field": "advertised_url"}) from exc

        if self._config.require_source_ip_match and source_ip not in target.addresses:
            message = (
                f"This enrolment came from {source_ip}, but {target.host} resolves to "
                f"{', '.join(target.addresses)}."
            )
            await self._fail(enrollment, message, target.url)
            raise ValidationError(message, details={"field": "advertised_url"})

        try:
            health = await self._nodes_service.probe_agent(
                agent_url=target.url, agent_token=agent_token, verify_tls=enrollment.verify_tls
            )
        except ValidationError as exc:
            # Only "reachable / not reachable" plus our own wording. The agent's response
            # body is never echoed back — that would turn a blind SSRF into a readable one.
            await self._fail(enrollment, str(exc.message), target.url)
            raise

        # The agent's own answer is the authority. A `node_name` in the request body is
        # only what the script *believes*, and an earlier version let it override the
        # probe — which produced an error message blaming the agent for a name the caller
        # had supplied. Both are checked; neither is allowed to speak for the other.
        reported = health.get("node_name")
        if (
            reported_name
            and reported_name != enrollment.node_name
            and not self._config.allow_node_name_mismatch
        ):
            message = (
                f"This enrolment is for {enrollment.node_name!r}, but the request asked to "
                f"enrol {reported_name!r}. The name is fixed when the enrolment is issued."
            )
            await self._fail(enrollment, message, target.url)
            raise ValidationError(message, details={"field": "node_name"})
        if (
            reported
            and reported != enrollment.node_name
            and not self._config.allow_node_name_mismatch
        ):
            # Closes a gap the agent's own config has documented all along: NODE_AGENT_
            # NODE_NAME "must match the node registered in the control plane", and until
            # now nothing checked. Enrolment is the moment to catch it — the operator is
            # present, nothing has been created, and the fix is one environment variable.
            message = (
                f"The agent at {target.url} reports node_name={reported!r}, but this "
                f"enrolment is for {enrollment.node_name!r}. Set "
                f"NODE_AGENT_NODE_NAME={enrollment.node_name} and restart the agent."
            )
            await self._fail(enrollment, message, target.url)
            raise ValidationError(message, details={"field": "node_name"})

        existing = await self._nodes.get(enrollment.node_id) if enrollment.node_id else None
        if existing is None and await self._nodes.get_by_name(enrollment.node_name):
            message = f"A node named {enrollment.node_name!r} was registered in the meantime."
            await self._fail(enrollment, message, target.url)
            raise ConflictError(message, details={"field": "name"})

        # From here on it is one transaction: claim the token and create the node together.
        if not await self._enrollments.claim(enrollment.token_hash, source_ip=source_ip):
            raise ConflictError(_REJECTED)

        enrollment.advertised_url = target.url
        enrollment.resolved_ip = target.addresses[0] if target.addresses else None

        labels = dict(enrollment.labels or {})
        if self._config.enroll_disabled:
            # Reuses the label `list_pollable` already honours: the node is visible but
            # inert until an administrator clears it. A two-person control from parts
            # that already exist.
            labels["disabled"] = "true"

        node, result = await self._nodes_service.accept_node(
            name=enrollment.node_name,
            agent_url=target.url,
            agent_token=agent_token,
            health=health,
            description=enrollment.description,
            verify_tls=enrollment.verify_tls,
            labels=labels,
            existing=existing,
            actor=None,
            action=(AuditAction.NODE_REENROLLED if existing else AuditAction.NODE_ENROLLED),
            audit_metadata={
                "enrollment": str(enrollment.id),
                "prefix": enrollment.token_prefix,
                "source_ip": source_ip,
                "resolved_ip": enrollment.resolved_ip,
            },
        )
        enrollment.node_id = node.id

        if source_ip and source_ip not in target.addresses:
            # Recorded, not refused: NAT, jump hosts and bridge networks all produce
            # legitimate mismatches, and enforcing by default would break enrolment in
            # exactly the networks this platform ships into.
            log.warning(
                "enrollment_source_ip_mismatch",
                node=enrollment.node_name,
                source_ip=source_ip,
                resolved=list(target.addresses),
            )
        return node, result

    # -- housekeeping ------------------------------------------------------
    async def sweep(self) -> tuple[int, int]:
        """Expire elapsed invitations and delete long-settled ones."""
        expired = await self._enrollments.expire_stale()
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=self._config.retention_days)
        return expired, await self._enrollments.purge_settled(cutoff)

    # -- internals ---------------------------------------------------------
    def _from_source(self, source_ip: str | None) -> str:
        """Fall back to where the request came from when the node did not say.

        A convenience for the flat-network case, and only ever a fallback: behind NAT the
        source address is the gateway, not the node, so guessing it by default would
        cheerfully register the firewall as a GPU host.
        """
        if not source_ip:
            raise ValidationError(
                "No agent address was given and it could not be inferred from the request.",
                details={"field": "advertised_url"},
            )
        host = f"[{source_ip}]" if ":" in source_ip else source_ip
        return f"http://{host}:{self._config.default_agent_port}"

    async def _fail(self, enrollment: NodeEnrollment, error: str, url: str | None) -> None:
        """Record a failed attempt on a session of its own.

        The caller is about to raise, which rolls this request back. Without an
        independent session the attempt counter would never survive — and that counter is
        what bounds how many outbound probes one token can cause.
        """
        await self._audit.record_independent(
            AuditAction.NODE_ENROLLMENT_FAILED,
            username="node",
            resource_type="node_enrollment",
            resource_id=str(enrollment.id),
            result=AuditResult.FAILURE,
            metadata={
                "name": enrollment.node_name,
                "prefix": enrollment.token_prefix,
                "url": url,
                "error": error[:500],
            },
        )
        if self._session_factory is None:  # pragma: no cover — always wired in the app
            return
        async with self._session_factory() as session:
            row = await session.get(NodeEnrollment, enrollment.id)
            if row is None:
                return
            row.attempts += 1
            row.last_attempt_at = dt.datetime.now(dt.UTC)
            row.last_error = error[:500]
            row.advertised_url = url
            if row.attempts >= self._config.max_attempts_per_token:
                # Burnt, not merely counted: a token that has failed this many times is
                # either being probed or is hopelessly misconfigured, and in both cases
                # an administrator should have to look at it.
                row.status = EnrollmentStatus.REVOKED
                row.last_error = f"{error[:400]} (revoked after {row.attempts} attempts)"
            await session.commit()

    async def _record_denied(self, enrollment: NodeEnrollment | None, reason: str) -> None:
        await self._audit.record_independent(
            AuditAction.NODE_ENROLLMENT_REUSED
            if reason == "token already used"
            else AuditAction.NODE_ENROLLMENT_FAILED,
            username="node",
            resource_type="node_enrollment",
            resource_id=str(enrollment.id) if enrollment else None,
            result=AuditResult.DENIED,
            metadata={
                "reason": reason,
                # The prefix identifies which invitation was presented without recording
                # anything usable. An unknown token contributes nothing at all.
                "prefix": enrollment.token_prefix if enrollment else None,
            },
        )
