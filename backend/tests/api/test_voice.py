"""Voice sessions and configuration (M29).

Weighted towards the things that are quiet when wrong.

**Retention.** Both switches are off by default and a site turns them on deliberately.
A bug that stores a transcript anyway is invisible — everything works, and the platform
is keeping what somebody said when it was told not to.

**Ownership.** A voice session carries what a person said out loud. Reading someone
else's is the same act as reading their chat history, not a lesser one.

**Config resolution.** Overrides are stored as a diff against the environment, so a
bundle that later improves a default is not silently pinned to today's value.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import VoiceSettings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.permissions import Permission as Perm
from app.models.agents import Agent, AgentVersion
from app.models.auth import User
from app.models.voice import VoiceSessionState
from app.repositories.agents import AgentRepository
from app.repositories.voice import (
    VoiceEventRepository,
    VoiceMessageRepository,
    VoiceSessionRepository,
)
from app.services.voice import VoiceSessionService
from app.services.voice_config import VoiceConfigStore
from tests.api.conftest import _user_with


@pytest.fixture
async def speaker(session: AsyncSession, settings) -> User:
    return await _user_with(session, settings, [Perm.AGENT_EXECUTE])


@pytest.fixture
async def other_person(session: AsyncSession, settings) -> User:
    return await _user_with(session, settings, [Perm.AGENT_EXECUTE])


@pytest.fixture
async def voice_agent(session: AsyncSession) -> Agent:
    agent = Agent(slug="voice-agent", display_name="Voice Agent", enabled=True)
    session.add(agent)
    await session.flush()
    session.add(
        AgentVersion(agent_id=agent.id, version=1, system_prompt="p", model="enterprise-chat")
    )
    await session.flush()
    return agent


def _service(session: AsyncSession, config: VoiceSettings) -> VoiceSessionService:
    return VoiceSessionService(
        config,
        VoiceSessionRepository(session),
        VoiceMessageRepository(session),
        VoiceEventRepository(session),
        AgentRepository(session),
    )


class TestSessionCreation:
    async def test_disabled_is_refused_with_a_reason(
        self, session: AsyncSession, speaker: User, voice_agent: Agent
    ) -> None:
        """Not a socket that fails at the first utterance."""
        service = _service(session, VoiceSettings(enabled=False))
        with pytest.raises(ConflictError, match="disabled"):
            await service.create(actor=speaker, agent_slug="voice-agent", language=None, voice=None)

    async def test_no_agent_and_no_default_is_refused(
        self, session: AsyncSession, speaker: User
    ) -> None:
        service = _service(session, VoiceSettings(enabled=True))
        with pytest.raises(ValidationError, match="No agent"):
            await service.create(actor=speaker, agent_slug=None, language=None, voice=None)

    async def test_the_default_agent_is_used_when_none_is_named(
        self, session: AsyncSession, speaker: User, voice_agent: Agent
    ) -> None:
        service = _service(session, VoiceSettings(enabled=True, default_agent_slug="voice-agent"))
        created = await service.create(actor=speaker, agent_slug=None, language=None, voice=None)
        assert created.agent_id == voice_agent.id
        assert created.state == VoiceSessionState.CREATED

    async def test_a_session_gets_its_own_conversation(
        self, session: AsyncSession, speaker: User, voice_agent: Agent
    ) -> None:
        """So the second question resolves against the first (§24)."""
        service = _service(session, VoiceSettings(enabled=True))
        created = await service.create(
            actor=speaker, agent_slug="voice-agent", language=None, voice=None
        )
        assert created.conversation_id


class TestPrivacy:
    async def test_a_transcript_is_not_stored_when_retention_is_off(
        self, session: AsyncSession, speaker: User, voice_agent: Agent
    ) -> None:
        """The default. The turn is still recorded — what was said is not."""
        service = _service(session, VoiceSettings(enabled=True, store_transcripts=False))
        created = await service.create(
            actor=speaker, agent_slug="voice-agent", language=None, voice=None
        )
        message = await service.record_message(
            created, role="user", text="my employee number is 12345"
        )
        assert message.text is None
        # The turn itself still exists, which is what an operator needs to see.
        assert message.sequence == 1
        assert created.turns == 1

    async def test_a_transcript_is_stored_when_retention_is_on(
        self, session: AsyncSession, speaker: User, voice_agent: Agent
    ) -> None:
        service = _service(session, VoiceSettings(enabled=True, store_transcripts=True))
        created = await service.create(
            actor=speaker, agent_slug="voice-agent", language=None, voice=None
        )
        message = await service.record_message(created, role="user", text="hello")
        assert message.text == "hello"

    async def test_another_persons_session_is_not_readable(
        self,
        session: AsyncSession,
        speaker: User,
        other_person: User,
        voice_agent: Agent,
    ) -> None:
        """404, not 403: whether a session exists is itself something to withhold."""
        service = _service(session, VoiceSettings(enabled=True))
        created = await service.create(
            actor=speaker, agent_slug="voice-agent", language=None, voice=None
        )
        with pytest.raises(NotFoundError):
            await service.get(created.id, actor=other_person)


@pytest.fixture
async def no_stored_config(session: AsyncSession):
    """Start from "nothing stored", whatever this platform has been configured with.

    The store reads a real table, and a developer machine has usually had the assistant
    configured through the admin console at some point. Rolled back with the test, so the
    running platform keeps its settings.
    """
    from sqlalchemy import delete

    from app.models.system import SystemSetting
    from app.services.voice_config import VOICE_SETTINGS_KEY

    await session.execute(delete(SystemSetting).where(SystemSetting.key == VOICE_SETTINGS_KEY))
    await session.flush()


@pytest.mark.usefixtures("no_stored_config")
class TestConfigStore:
    async def test_the_environment_is_used_when_nothing_is_stored(
        self, session: AsyncSession, settings
    ) -> None:
        store = VoiceConfigStore(session, settings)
        assert (await store.get()).model_dump() == settings.voice.model_dump()

    async def test_an_override_is_merged_over_the_environment(
        self, session: AsyncSession, settings
    ) -> None:
        """A field nobody touched keeps following the bundle's default."""
        store = VoiceConfigStore(session, settings)
        updated = await store.update({"enabled": True})
        assert updated.enabled is True
        # Untouched, so still whatever the environment says.
        assert updated.stt_model == settings.voice.stt_model

    async def test_only_the_difference_is_persisted(self, session: AsyncSession, settings) -> None:
        """Storing the whole object would pin every field at today's default, so a later
        bundle that improves one of them would be silently ignored."""
        from sqlalchemy import select

        from app.models.system import SystemSetting
        from app.services.voice_config import VOICE_SETTINGS_KEY

        store = VoiceConfigStore(session, settings)
        await store.update({"enabled": True})
        row = (
            await session.execute(
                select(SystemSetting).where(SystemSetting.key == VOICE_SETTINGS_KEY)
            )
        ).scalar_one()
        assert row.value == {"enabled": True}

    async def test_an_invalid_update_is_refused(self, session: AsyncSession, settings) -> None:
        store = VoiceConfigStore(session, settings)
        with pytest.raises(ValidationError):
            await store.update({"default_language": "klingon"})

    async def test_a_stored_override_that_no_longer_validates_falls_back(
        self, session: AsyncSession, settings
    ) -> None:
        """A field removed in an upgrade must not stop the assistant starting: the
        environment is a working configuration by definition."""
        from app.models.system import SystemSetting
        from app.services.voice_config import VOICE_SETTINGS_KEY

        session.add(
            SystemSetting(
                key=VOICE_SETTINGS_KEY,
                value={"default_language": "nonsense"},
                category="voice",
            )
        )
        await session.flush()
        store = VoiceConfigStore(session, settings)
        assert (await store.get()).default_language == settings.voice.default_language
