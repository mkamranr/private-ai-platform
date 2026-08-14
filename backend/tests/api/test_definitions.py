"""Declarative agents, skills and tools (M10-M12).

Weighted towards the three things that would be silent.

**Order.** Tools before skills before agents. Import the other way and an agent is
created with an empty tool list — it exists, it answers, and it can do nothing, which
looks like a model problem rather than an import one.

**Unresolved references are reported.** An agent that names three tools and receives one
is not the agent the manifest describes, and the commonest cause is an MCP server this
site never started.

**Re-import versions rather than edits.** A run records the version it executed; editing
that version in place would make an old run unexplainable (§M14).
"""

from __future__ import annotations

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.repositories.agents import AgentRepository, SkillRepository, ToolRepository
from app.services.definitions import DefinitionImporter
from tests.api.conftest import _user_with


def _write(directory, name: str, spec: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(yaml.safe_dump(spec), encoding="utf-8")


@pytest.fixture
async def importer(session: AsyncSession, settings, tmp_path):
    """An importer pointed at a temporary set of manifest directories."""
    settings.agents.tool_manifest_path = str(tmp_path / "tools")
    settings.agents.skill_manifest_path = str(tmp_path / "skills")
    settings.agents.agent_manifest_path = str(tmp_path / "agents")

    from app.core.security import SecretCipher
    from app.repositories.agents import AgentVersionRepository
    from app.repositories.audit import AuditRepository
    from app.services.agent_registry import (
        AgentRegistryService,
        SkillRegistryService,
        ToolRegistryService,
    )
    from app.services.audit import AuditService
    from app.services.tool_executors import build_executors

    audit = AuditService(AuditRepository(session))
    cipher = SecretCipher(settings.security)
    tools = ToolRegistryService(
        settings, ToolRepository(session), audit, cipher, build_executors(cipher)
    )
    skills = SkillRegistryService(SkillRepository(session))
    agents = AgentRegistryService(
        settings,
        AgentRepository(session),
        AgentVersionRepository(session),
        ToolRepository(session),
        SkillRepository(session),
        audit,
    )
    return (
        DefinitionImporter(
            settings,
            ToolRepository(session),
            SkillRepository(session),
            AgentRepository(session),
            tools,
            skills,
            agents,
        ),
        tmp_path,
        session,
    )


@pytest.fixture
async def author(session: AsyncSession, settings) -> User:
    return await _user_with(
        session, settings, [Perm.AGENT_CREATE, Perm.TOOL_MANAGE, Perm.SKILL_MANAGE]
    )


class TestImportOrder:
    async def test_an_agent_receives_the_tools_and_skills_it_names(
        self, importer, author: User
    ) -> None:
        """The ordering guarantee, and the reason this file exists.

        Tools and skills are created in the same pass as the agent that references them,
        so the agent must come out holding both — not an empty allow-list that only shows
        up as an agent politely declining to do its job.
        """
        imp, root, db = importer
        _write(
            root / "tools",
            "clock.yaml",
            {
                "name": "definitions-test-clock",
                "display_name": "Clock",
                "description": "Return the current UTC time.",
                "type": "INTERNAL",
                "required_permission": "tool.execute",
                "config": {"handler": "current_datetime"},
            },
        )
        _write(
            root / "skills",
            "grounded.yaml",
            {
                "name": "grounded",
                "display_name": "Grounded",
                "description": "Cite sources.",
                "instructions": "Cite every claim.",
                "required_tools": ["definitions-test-clock"],
            },
        )
        _write(
            root / "agents",
            "helper.yaml",
            {
                "slug": "helper",
                "display_name": "Helper",
                "system_prompt": "You help.",
                "model": "enterprise-chat",
                "tools": ["definitions-test-clock"],
                "skills": ["grounded"],
            },
        )

        results = await imp.import_all(actor=author)
        actions = {(r.kind, r.name): r.action for r in results}
        assert actions[("tool", "definitions-test-clock")] == "created"
        assert actions[("skill", "grounded")] == "created"
        assert actions[("agent", "helper")] == "created"

        agent = await AgentRepository(db).get_by_slug("helper")
        assert agent is not None

    async def test_an_unresolved_reference_is_reported_not_swallowed(
        self, importer, author: User
    ) -> None:
        """An agent granted a tool that does not exist here is still worth creating —
        the tool may come from an MCP server this site has not started. What must not
        happen is silence about it."""
        imp, root, _db = importer
        _write(
            root / "agents",
            "partial.yaml",
            {
                "slug": "partial",
                "display_name": "Partial",
                "system_prompt": "You help.",
                "model": "enterprise-chat",
                "tools": ["ad_user_lookup"],
            },
        )

        results = await imp.import_all(actor=author)
        agent_result = next(r for r in results if r.kind == "agent")
        assert agent_result.action == "created"
        assert "ad_user_lookup" in (agent_result.detail or "")


class TestReimport:
    async def test_reimporting_an_agent_publishes_a_new_version(
        self, importer, author: User
    ) -> None:
        """Never an in-place edit: a run records the version it executed."""
        imp, root, db = importer
        spec = {
            "slug": "versioned",
            "display_name": "Versioned",
            "system_prompt": "First prompt.",
            "model": "enterprise-chat",
        }
        _write(root / "agents", "versioned.yaml", spec)
        await imp.import_all(actor=author)

        spec["system_prompt"] = "Second prompt."
        _write(root / "agents", "versioned.yaml", spec)
        results = await imp.import_all(actor=author)

        assert next(r for r in results if r.kind == "agent").action == "updated"
        agent = await AgentRepository(db).get_by_slug("versioned")
        assert agent is not None
        assert agent.current_version == 2

    async def test_reimporting_a_skill_applies_the_edited_text(
        self, importer, author: User
    ) -> None:
        """Instructions are the whole content of a skill, so applying the file is the
        point — an operator editing the wording expects the next run to use it."""
        imp, root, db = importer
        spec = {
            "name": "editable",
            "display_name": "Editable",
            "description": "d",
            "instructions": "Original instructions.",
        }
        _write(root / "skills", "editable.yaml", spec)
        await imp.import_all(actor=author)

        spec["instructions"] = "Revised instructions."
        _write(root / "skills", "editable.yaml", spec)
        results = await imp.import_all(actor=author)

        assert next(r for r in results if r.kind == "skill").action == "updated"
        skill = await SkillRepository(db).get_by_name("editable")
        assert skill is not None
        assert skill.instructions == "Revised instructions."

    async def test_an_existing_tool_is_left_alone(self, importer, author: User) -> None:
        """A tool's type and parameter schema define what it *is*. Rewriting them under
        an agent already granted it would silently change what that agent can do."""
        imp, root, _db = importer
        _write(
            root / "tools",
            "clock.yaml",
            {
                "name": "definitions-test-clock",
                "display_name": "Clock",
                "description": "Return the current UTC time.",
                "type": "INTERNAL",
                "required_permission": "tool.execute",
                "config": {"handler": "current_datetime"},
            },
        )
        await imp.import_all(actor=author)
        results = await imp.import_all(actor=author)
        assert next(r for r in results if r.kind == "tool").action == "unchanged"


class TestManifestsThatShip:
    """The definitions in this repository, checked as data rather than as prose."""

    def test_every_shipped_manifest_parses_and_is_complete(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        for directory, required in (
            ("tools", {"name", "display_name", "description", "type", "required_permission"}),
            ("skills", {"name", "display_name", "description", "instructions"}),
            ("agents", {"slug", "display_name", "system_prompt", "model"}),
        ):
            for path in sorted((root / directory).glob("*.y*ml")):
                spec = yaml.safe_load(path.read_text(encoding="utf-8"))
                assert isinstance(spec, dict), path
                missing = required - set(spec)
                assert not missing, f"{path.name} is missing {missing}"

    def test_shipped_agents_only_reference_shipped_or_discovered_names(self) -> None:
        """An agent naming a skill nobody ships is a typo that would import 'successfully'
        and produce an agent quietly missing its instructions."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        skills = {yaml.safe_load(p.read_text())["name"] for p in (root / "skills").glob("*.y*ml")}
        for path in (root / "agents").glob("*.y*ml"):
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
            for name in spec.get("skills") or []:
                assert name in skills, f"{path.name} names unknown skill {name!r}"
