"""RBAC catalogue coherence and audit redaction (M03, M24)."""

from __future__ import annotations

import pytest

from app.core.permissions import (
    PERMISSION_CATALOGUE,
    ROLE_DEFINITIONS,
    Permission,
    Role,
    split_permission,
)
from app.services.audit import redact


class TestPermissionCatalogue:
    def test_every_constant_is_in_the_catalogue(self) -> None:
        """A permission constant absent from the catalogue is never seeded, so any
        route requiring it would be unreachable by every role."""
        constants = {
            value
            for name, value in vars(Permission).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        assert constants - set(PERMISSION_CATALOGUE) == set()

    def test_all_names_are_resource_dot_action(self) -> None:
        for name in PERMISSION_CATALOGUE:
            resource, action = split_permission(name)
            assert resource and action
            assert name == f"{resource}.{action}"

    def test_no_duplicate_resource_action_pairs(self) -> None:
        """The permissions table has a UNIQUE(resource, action) constraint; a
        duplicate here would make seeding fail on a fresh install."""
        pairs = [split_permission(name) for name in PERMISSION_CATALOGUE]
        assert len(pairs) == len(set(pairs))

    def test_every_permission_has_a_description(self) -> None:
        assert [n for n, d in PERMISSION_CATALOGUE.items() if not d.strip()] == []

    @pytest.mark.parametrize("bad", ["nodot", "", ".leading", "trailing."])
    def test_malformed_permission_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match=r"resource\.action"):
            split_permission(bad)


class TestRoleDefinitions:
    def test_all_eight_spec_roles_defined(self) -> None:
        expected = {
            value
            for name, value in vars(Role).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        assert set(ROLE_DEFINITIONS) == expected
        assert len(ROLE_DEFINITIONS) == 8

    def test_roles_grant_only_catalogued_permissions(self) -> None:
        """An uncatalogued grant would be silently dropped during seeding."""
        for role, (_, permissions) in ROLE_DEFINITIONS.items():
            unknown = permissions - set(PERMISSION_CATALOGUE)
            assert unknown == set(), f"{role} grants unknown permissions: {unknown}"

    def test_super_admin_holds_everything(self) -> None:
        _, permissions = ROLE_DEFINITIONS[Role.SUPER_ADMIN]
        assert permissions == frozenset(PERMISSION_CATALOGUE)

    def test_separation_of_duty_holds(self) -> None:
        """The point of eight roles is separation of duty. If each were a near-copy
        of ADMIN the model would be decorative, so the key exclusions are asserted."""
        infra = ROLE_DEFINITIONS[Role.INFRA_ADMIN][1]
        agent = ROLE_DEFINITIONS[Role.AGENT_ADMIN][1]
        auditor = ROLE_DEFINITIONS[Role.AUDITOR][1]
        user = ROLE_DEFINITIONS[Role.USER][1]

        # Infrastructure admins run the hardware; they do not author agents.
        assert Permission.AGENT_CREATE not in infra
        assert Permission.MODEL_DEPLOY not in infra
        # Agent admins do not touch infrastructure or deploy models.
        assert Permission.INFRASTRUCTURE_MANAGE not in agent
        assert Permission.MODEL_DEPLOY not in agent
        # Auditors read; they change nothing.
        assert Permission.USER_MANAGE not in auditor
        assert Permission.AGENT_CREATE not in auditor
        assert Permission.MODEL_DEPLOY not in auditor
        # Ordinary users cannot manage anything or read the audit log.
        assert Permission.USER_MANAGE not in user
        assert Permission.AUDIT_VIEW not in user
        assert Permission.TOOL_MANAGE not in user

    def test_only_super_admin_can_manage_backups(self) -> None:
        holders = {r for r, (_, p) in ROLE_DEFINITIONS.items() if Permission.BACKUP_MANAGE in p}
        assert holders == {Role.SUPER_ADMIN}

    def test_tool_approval_is_not_granted_with_mere_execution(self) -> None:
        """Approving a HIGH-risk action is a different privilege from performing a
        routine one (§10). Any role that could approve its own calls defeats the
        approval workflow."""
        for role, (_, permissions) in ROLE_DEFINITIONS.items():
            if role in {Role.SUPER_ADMIN, Role.ADMIN, Role.AGENT_ADMIN}:
                continue
            assert Permission.TOOL_APPROVE not in permissions, role


class TestAuditRedaction:
    @pytest.mark.parametrize(
        "key",
        ["password", "secret", "token", "api_key", "bind_password", "encryption_key"],
    )
    def test_sensitive_keys_redacted(self, key: str) -> None:
        assert redact({key: "sensitive"})[key] == "[REDACTED]"

    def test_case_insensitive(self) -> None:
        assert redact({"PASSWORD": "x", "Api_Key": "y"}) == {
            "PASSWORD": "[REDACTED]",
            "Api_Key": "[REDACTED]",
        }

    def test_nested_structures_redacted(self) -> None:
        """Tool arguments arrive nested, so a shallow scrub would miss them."""
        payload = {
            "tool": "ldap.search",
            "config": {"bind_dn": "cn=svc", "bind_password": "hunter2"},
            "attempts": [{"password": "first"}, {"password": "second"}],
        }
        cleaned = redact(payload)
        assert cleaned["config"]["bind_password"] == "[REDACTED]"
        assert cleaned["attempts"][0]["password"] == "[REDACTED]"
        assert cleaned["attempts"][1]["password"] == "[REDACTED]"
        # Non-sensitive values survive — the record must stay useful.
        assert cleaned["tool"] == "ldap.search"
        assert cleaned["config"]["bind_dn"] == "cn=svc"

    def test_non_sensitive_values_preserved(self) -> None:
        payload = {"model": "qwen3-30b", "gpu_ids": [0, 1], "count": 2, "ok": True}
        assert redact(payload) == payload

    def test_deeply_nested_input_terminates(self) -> None:
        """Depth cap guards against pathological or malicious nesting."""
        deep: dict = {"password": "x"}
        for _ in range(50):
            deep = {"nested": deep}
        redact(deep)  # must not recurse without bound

    def test_scalars_pass_through(self) -> None:
        assert redact("plain") == "plain"
        assert redact(42) == 42
        assert redact(None) is None
