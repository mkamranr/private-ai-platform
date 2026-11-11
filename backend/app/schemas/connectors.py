"""Connector catalogue (E10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel

ConnectorState = Literal["available", "registered", "installed", "unreachable", "invalid"]


class ConnectorConfigItem(BaseModel):
    name: str
    description: str
    secret: bool
    required: bool


class ConnectorServer(BaseModel):
    id: uuid.UUID
    status: str
    status_detail: str | None
    enabled: bool
    last_checked_at: dt.datetime | None
    last_discovered_at: dt.datetime | None


class ConnectorTools(BaseModel):
    total: int
    enabled: int
    #: Waiting for someone to enable them (applying a suggested risk does not count).
    disabled: int
    #: Tools still at the discovery default whose risk the manifest would change.
    suggestions_pending: int


class Connector(BaseModel):
    name: str
    display_name: str
    description: str
    category: str | None
    network: str | None
    writes: bool | None
    package: str | None
    package_kind: str | None
    source: str | None
    file: str
    endpoint: str
    volumes: list[str]
    config: list[ConnectorConfigItem]
    suggested_risk: dict[str, str]
    problems: list[str]
    state: ConnectorState
    server: ConnectorServer | None
    tools: ConnectorTools


class ConnectorCategory(BaseModel):
    id: str
    label: str


class ConnectorCatalogue(BaseModel):
    manifest_path: str
    categories: list[ConnectorCategory]
    items: list[Connector]


class ConnectorInstallResult(BaseModel):
    connector: Connector
    found: int
    created: int
    updated: int
    detail: str | None


class ConnectorRiskResult(BaseModel):
    connector: Connector
    changed: int
    skipped_reviewed: int
