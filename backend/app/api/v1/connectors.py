"""The connector catalogue (expansion E10). See `app.services.connector_catalogue`."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import ConnectorCatalogueDep, require_permission
from app.api.routing import CommittingRoute
from app.core.permissions import Permission as Perm
from app.models.auth import User
from app.schemas.connectors import (
    Connector,
    ConnectorCatalogue,
    ConnectorInstallResult,
    ConnectorRiskResult,
)

router = APIRouter(prefix="/mcp/catalogue", tags=["mcp"], route_class=CommittingRoute)

NamePath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")]


@router.get("", response_model=ConnectorCatalogue, summary="The connector catalogue")
async def catalogue(
    service: ConnectorCatalogueDep,
    _actor: Annotated[User, require_permission(Perm.MCP_VIEW)],
) -> ConnectorCatalogue:
    """Every connector in `mcp/manifests/`, with what it needs, whether it is valid, and
    whether it is installed and answering."""
    return ConnectorCatalogue.model_validate(await service.catalogue())


@router.get("/{name}", response_model=Connector, summary="One connector")
async def connector(
    service: ConnectorCatalogueDep,
    _actor: Annotated[User, require_permission(Perm.MCP_VIEW)],
    name: NamePath,
) -> Connector:
    return Connector.model_validate(await service.describe(name))


@router.post(
    "/{name}/install", response_model=ConnectorInstallResult, summary="Install a connector"
)
async def install(
    service: ConnectorCatalogueDep,
    actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
    name: NamePath,
) -> ConnectorInstallResult:
    """Register the connector's server and discover its tools. Discovered tools arrive
    disabled at HIGH risk; installing grants nothing. 422 for a manifest that breaks the
    catalogue rules."""
    result = await service.install(name, actor=actor)
    await service.commit()
    return ConnectorInstallResult.model_validate(result)


@router.post(
    "/{name}/apply-suggested-risk",
    response_model=ConnectorRiskResult,
    summary="Apply the manifest's suggested risk levels",
)
async def apply_suggested_risk(
    service: ConnectorCatalogueDep,
    actor: Annotated[User, require_permission(Perm.MCP_MANAGE)],
    name: NamePath,
) -> ConnectorRiskResult:
    """Only tools nobody has reviewed yet (disabled, at the discovery risk level). Enables
    nothing. Audited with every change."""
    result = await service.apply_suggested_risk(name, actor=actor)
    await service.commit()
    return ConnectorRiskResult.model_validate(result)
