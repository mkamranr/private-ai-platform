"""Federation rules — where an external claim becomes platform access (M03, Phase 6).

These are the tests that matter most in Phase 6. Every one of them is a way an external
directory could otherwise take over the platform, and none of them is visible from the
happy path: a site that only ever tests "an AD user can sign in" passes while every rule
below is broken.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import FederationSettings, Settings
from app.core.errors import AuthenticationError
from app.core.interfaces.auth_provider import ExternalIdentity
from app.core.security import PasswordHasherService
from app.models.auth import Permission, Role, User
from app.repositories.audit import AuditRepository
from app.repositories.user import RoleRepository, UserRepository
from app.services.audit import AuditService
from app.services.federation import FederationService

pytestmark = pytest.mark.asyncio


def _identity(**overrides: object) -> ExternalIdentity:
    base = {
        "subject": "S-1-5-21-" + uuid.uuid4().hex[:8],
        "username": "fatima-" + uuid.uuid4().hex[:6],
        "email": f"fatima-{uuid.uuid4().hex[:6]}@example.ae",
        "provider": "ldap",
        "full_name": "Fatima Al Mansoori",
        "groups": ("AI-Platform-Users",),
    }
    base.update(overrides)
    return ExternalIdentity(**base)  # type: ignore[arg-type]


async def _role(session: AsyncSession, name: str) -> Role:
    existing = (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if existing is not None:
        return existing
    suffix = uuid.uuid4().hex[:8]
    permission = Permission(
        name=f"agent.view.{suffix}", resource=f"agent{suffix}", action="view", description="t"
    )
    role = Role(name=name, description="test", permissions=[permission])
    session.add(role)
    await session.flush()
    return role


def _service(session: AsyncSession, settings: Settings, **overrides: object) -> FederationService:
    return FederationService(
        UserRepository(session),
        RoleRepository(session),
        AuditService(AuditRepository(session), settings),
        FederationSettings(**overrides),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Rule 1 — match on subject, not username
# ---------------------------------------------------------------------------
async def test_a_reused_username_does_not_inherit_the_previous_holders_account(
    session: AsyncSession, settings: Settings
) -> None:
    """The leaver/joiner case. `j.smith` leaves; a new J. Smith joins and gets the same
    username in AD. The directory gives them a *different* subject, so they must get a
    different platform account — not the previous holder's roles and audit history."""
    await _role(session, "USER")
    service = _service(session, settings, default_roles=["USER"])

    leaver = await service.resolve(
        _identity(subject="OLD-SUBJECT", username="j.smith", email="j.smith@example.ae")
    )
    leaver_id = leaver.id

    # Refused rather than silently reusing: usernames are unique platform-wide, so a
    # human has to decide which name each person keeps.
    with pytest.raises(AuthenticationError, match="already belongs to a different"):
        await service.resolve(
            _identity(subject="NEW-SUBJECT", username="j.smith", email="jsmith2@example.ae")
        )

    survivor = (await session.execute(select(User).where(User.id == leaver_id))).scalar_one()
    assert survivor.external_subject == "OLD-SUBJECT", (
        "The joiner overwrote the previous holder's directory subject, taking over their account."
    )


async def test_the_same_subject_returns_the_same_account_even_if_renamed(
    session: AsyncSession, settings: Settings
) -> None:
    """The marriage/rename case: same person, new username. Must stay one account."""
    await _role(session, "USER")
    service = _service(session, settings, default_roles=["USER"])

    before = await service.resolve(
        _identity(subject="STABLE", username="f.almansoori", email="f.almansoori@example.ae")
    )
    after = await service.resolve(
        _identity(subject="STABLE", username="f.alhosani", email="f.alhosani@example.ae")
    )

    assert before.id == after.id


# ---------------------------------------------------------------------------
# Rule 2 — never federate over a local account
# ---------------------------------------------------------------------------
async def test_a_directory_user_cannot_take_over_a_local_account(
    session: AsyncSession, settings: Settings
) -> None:
    """The break-glass account is the one that still works when the IdP is down. If a
    directory entry named `admin` could claim it, anyone who can create a directory entry
    owns the platform."""
    hasher = PasswordHasherService(settings.security)
    local = User(
        username="admin-local-" + uuid.uuid4().hex[:6],
        email=f"admin-{uuid.uuid4().hex[:6]}@ai-platform.local",
        hashed_password=hasher.hash("irrelevant-for-this-test"),
        auth_provider="local",
        is_superuser=True,
    )
    session.add(local)
    await session.flush()

    service = _service(session, settings)
    with pytest.raises(AuthenticationError, match="local platform account"):
        await service.resolve(_identity(username=local.username))

    await session.refresh(local)
    assert local.auth_provider == "local"
    assert local.external_subject is None
    assert local.is_superuser is True


# ---------------------------------------------------------------------------
# Rule 3 — a group can never grant SUPER_ADMIN
# ---------------------------------------------------------------------------
async def test_a_group_mapped_to_super_admin_is_refused(
    session: AsyncSession, settings: Settings
) -> None:
    """SUPER_ADMIN bypasses every permission check. Even an operator explicitly writing
    this mapping does not get it — otherwise anyone who can create the named group in AD
    escalates to full platform control."""
    await _role(session, "SUPER_ADMIN")
    await _role(session, "USER")
    service = _service(
        session,
        settings,
        role_mapping={"Domain Admins": "SUPER_ADMIN"},
        default_roles=["USER"],
    )

    user = await service.resolve(_identity(groups=("Domain Admins",)))

    assert "SUPER_ADMIN" not in {r.name for r in user.roles}
    assert user.is_superuser is False
    # No fallback to the default either: the group DID match a mapping, so substituting
    # USER would grant a role the operator never wrote. They sign in able to do nothing,
    # which is visible and safe.
    assert user.roles == []


async def test_group_mapping_is_case_insensitive_and_maps_to_configured_roles(
    session: AsyncSession, settings: Settings
) -> None:
    """AD group names are not case-stable across tools; an operator typing the name they
    see in one console must not silently get no roles."""
    await _role(session, "AGENT_ADMIN")
    service = _service(session, settings, role_mapping={"ai-platform-agent-admins": "AGENT_ADMIN"})

    user = await service.resolve(_identity(groups=("AI-Platform-Agent-Admins",)))

    assert {r.name for r in user.roles} == {"AGENT_ADMIN"}


# ---------------------------------------------------------------------------
# Rule 4 — removing a group removes access
# ---------------------------------------------------------------------------
async def test_losing_a_group_removes_the_role_on_next_sign_in(
    session: AsyncSession, settings: Settings
) -> None:
    """The whole reason to federate: revoking access in AD must revoke it here. Without
    this, the directory stops being authoritative the moment it is first read."""
    await _role(session, "AGENT_ADMIN")
    await _role(session, "USER")
    service = _service(
        session,
        settings,
        role_mapping={"agent-admins": "AGENT_ADMIN"},
        default_roles=["USER"],
        sync_roles_on_login=True,
    )

    identity = _identity(groups=("agent-admins",))
    promoted = await service.resolve(identity)
    assert {r.name for r in promoted.roles} == {"AGENT_ADMIN"}

    demoted = await service.resolve(
        _identity(
            subject=identity.subject, username=identity.username, email=identity.email, groups=()
        )
    )
    assert {r.name for r in demoted.roles} == {"USER"}
    assert demoted.id == promoted.id


async def test_sync_off_leaves_platform_managed_roles_alone(
    session: AsyncSession, settings: Settings
) -> None:
    """A site that manages roles in the platform must not have them overwritten by the
    directory on every sign-in."""
    await _role(session, "AGENT_ADMIN")
    await _role(session, "USER")
    service = _service(
        session,
        settings,
        role_mapping={"agent-admins": "AGENT_ADMIN"},
        default_roles=["USER"],
        sync_roles_on_login=False,
    )

    identity = _identity(groups=("agent-admins",))
    first = await service.resolve(identity)
    granted = {r.name for r in first.roles}

    again = await service.resolve(
        _identity(
            subject=identity.subject, username=identity.username, email=identity.email, groups=()
        )
    )
    assert {r.name for r in again.roles} == granted


# ---------------------------------------------------------------------------
# Provisioning policy
# ---------------------------------------------------------------------------
async def test_auto_provision_off_refuses_an_unknown_person(
    session: AsyncSession, settings: Settings
) -> None:
    service = _service(session, settings, auto_provision=False)
    with pytest.raises(AuthenticationError, match="no account"):
        await service.resolve(_identity())


async def test_a_federated_account_never_gets_a_password(
    session: AsyncSession, settings: Settings
) -> None:
    """A null hash is what makes `POST /auth/login` refuse the account. If provisioning
    ever set one, a directory account would become password-signable inside the platform,
    outliving the person's removal from the directory."""
    await _role(session, "USER")
    service = _service(session, settings, default_roles=["USER"])

    user = await service.resolve(_identity())

    assert user.hashed_password is None
    assert user.auth_provider == "ldap"
    assert user.is_superuser is False


async def test_a_disabled_platform_account_cannot_sign_in_via_the_directory(
    session: AsyncSession, settings: Settings
) -> None:
    """Disabling someone in the platform must lock them out even while AD still happily
    authenticates them — otherwise the platform's own off-switch does nothing."""
    await _role(session, "USER")
    service = _service(session, settings, default_roles=["USER"])

    identity = _identity()
    user = await service.resolve(identity)
    user.is_active = False
    await session.flush()

    with pytest.raises(AuthenticationError, match="disabled"):
        await service.resolve(identity)


async def test_an_unknown_role_in_the_mapping_is_skipped_not_fatal(
    session: AsyncSession, settings: Settings
) -> None:
    """A typo in FEDERATION__ROLE_MAPPING must not lock out everyone in that group."""
    await _role(session, "USER")
    service = _service(
        session, settings, role_mapping={"eng": "NO_SUCH_ROLE"}, default_roles=["USER"]
    )

    user = await service.resolve(_identity(groups=("eng",)))

    assert user.roles == []  # matched the mapping, so no default; but sign-in succeeded
