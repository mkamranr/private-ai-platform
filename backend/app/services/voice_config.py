"""Runtime voice configuration (M29, §49).

Voice settings are the first on this platform that an administrator changes **at run
time** rather than in `.env`. That is deliberate and narrow: choosing which speech models
the assistant uses, and whether recordings are kept, are decisions a site revisits — and
requiring a container restart to change them means they get set once, wrongly, and left.

The resolution order matters and is the same one `.env` already establishes:

    database override  >  environment / .env  >  field default

So a site can ship a bundle with sensible defaults, an administrator can adjust them from
the admin console, and clearing an override falls back to what the bundle shipped rather
than to nothing.

Stored as one JSONB row under a single key rather than a row per field. A partial write
then cannot leave the configuration half-applied — the assistant enabled while still
pointing at a model that was removed in the same edit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, VoiceSettings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.models.system import SystemSetting

log = get_logger(__name__)

#: One key, one row. See the module docstring.
VOICE_SETTINGS_KEY = "voice.config"


class VoiceConfigStore:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def get(self) -> VoiceSettings:
        """The configuration in force, environment defaults included.

        Overrides are merged *over* the environment rather than replacing it, so a field
        the administrator never touched keeps following the bundle's default instead of
        freezing at whatever it happened to be the first time somebody pressed Save.
        """
        row = await self._row()
        if row is None:
            return self._settings.voice

        merged = self._settings.voice.model_dump()
        merged.update(row.value or {})
        try:
            return VoiceSettings(**merged)
        except Exception:
            # A stored override that no longer validates — a field removed in an upgrade,
            # typically. The environment is a working configuration by definition, so the
            # assistant keeps running on it rather than failing to start.
            log.warning("voice_config_override_invalid", note="falling back to environment")
            return self._settings.voice

    async def update(self, patch: dict[str, Any]) -> VoiceSettings:
        """Apply a partial update and persist it.

        Validated by constructing the settings model, so a bad language code or a
        negative timeout is refused here rather than at the first session — where it
        would surface as a socket that closes for no visible reason.
        """
        current = await self.get()
        merged = current.model_dump()
        merged.update(patch)
        try:
            validated = VoiceSettings(**merged)
        except Exception as exc:
            raise ValidationError(f"Those voice settings are not valid: {exc}") from exc

        # Only what differs from the environment is stored. Persisting the whole object
        # would pin every field at today's default, so a later bundle that improves one
        # of them would be silently ignored.
        environment = self._settings.voice.model_dump()
        overrides = {
            key: value
            for key, value in validated.model_dump().items()
            if environment.get(key) != value
        }

        row = await self._row()
        if row is None:
            row = SystemSetting(
                key=VOICE_SETTINGS_KEY,
                value=overrides,
                category="voice",
                description="Voice assistant configuration (M29), set from the admin console.",
                is_system=True,
            )
            self._session.add(row)
        else:
            row.value = overrides
        await self._session.flush()
        log.info("voice_config_updated", fields=sorted(patch))
        return validated

    async def _row(self) -> SystemSetting | None:
        return (
            await self._session.execute(
                select(SystemSetting).where(SystemSetting.key == VOICE_SETTINGS_KEY)
            )
        ).scalar_one_or_none()
