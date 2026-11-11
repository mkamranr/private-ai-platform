"""Platform settings (expansion E8, plan §6). See `app.services.runtime_settings`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import RuntimeSettingsServiceDep, require_permission
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.runtime_settings import SettingReset, SettingsCatalogue, SettingUpdate

router = APIRouter(prefix="/settings", tags=["settings"])

KeyPath = Annotated[str, Path(pattern=r"^[a-z_]+\.[a-z0-9_]+$", max_length=128)]


@router.get("", response_model=SettingsCatalogue, summary="Runtime and deployment settings")
async def list_settings(
    service: RuntimeSettingsServiceDep,
    _actor: Annotated[User, require_permission(Perm.SETTINGS_VIEW)],
) -> SettingsCatalogue:
    """Runtime settings with their effective value and where it comes from, and a curated,
    redacted, read-only list of deployment settings."""
    return SettingsCatalogue.model_validate(await service.catalogue())


@router.put("/{key}", response_model=SettingsCatalogue, summary="Override a runtime setting")
async def update_setting(
    payload: SettingUpdate,
    service: RuntimeSettingsServiceDep,
    actor: Annotated[User, require_permission(Perm.SETTINGS_MANAGE)],
    key: KeyPath,
) -> SettingsCatalogue:
    """404 for anything that is not a runtime setting: deployment settings are not editable
    here by design. 422 when the value fails the same validation the environment gets."""
    await service.update(key, payload.value, actor=actor, reason=payload.reason)
    await service.commit()
    return SettingsCatalogue.model_validate(await service.catalogue())


@router.delete(
    "/{key}",
    response_model=SettingsCatalogue,
    status_code=status.HTTP_200_OK,
    summary="Return a runtime setting to its deployment value",
)
async def reset_setting(
    service: RuntimeSettingsServiceDep,
    actor: Annotated[User, require_permission(Perm.SETTINGS_MANAGE)],
    key: KeyPath,
    payload: SettingReset | None = None,
) -> SettingsCatalogue:
    await service.reset(key, actor=actor, reason=payload.reason if payload else None)
    await service.commit()
    return SettingsCatalogue.model_validate(await service.catalogue())
