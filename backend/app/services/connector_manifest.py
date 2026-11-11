"""MCP connector manifests: the catalogue fields and their rules (expansion E10).

A manifest in ``mcp/manifests/`` describes one connector: the pinned package an MCP server
comes from, how it runs, and (since E10) what the catalogue shows about it.

* ``category``: ``files``, ``data``, ``knowledge``, ``observability``, ``utilities`` or
  ``developer``.
* ``network``: ``none`` or ``internal``. There is no ``external``: a connector that needs
  the internet cannot work air-gapped, and would be a path out of the site if it could.
* ``writes``: whether any of its tools change something.
* ``config``: the ``.env`` variables (``MCP_...``) it reads, with a description;
  ``secrets`` and ``required`` list some of them. Secrets only ever come from ``.env``;
  one written into the manifest is refused, because manifests ship in the offline bundle.
* ``suggested_risk``: a risk level per tool name, a hint for the reviewer. It is applied
  only when an administrator chooses to, and never enables a tool.

``scripts/vendor_mcp_servers.py`` enforces the same rules on the build machine (it is
stdlib-only, so it cannot import this module). ``tests/services/test_connector_manifest.py``
runs both over every shipped manifest so they cannot drift apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
NPM_PINNED = re.compile(r"^@?[^@\s]+(/[^@\s]+)?@\d[^\s]*$")
PIP_PINNED = re.compile(r"^[A-Za-z0-9._-]+==\S+$")
CATEGORIES = ("files", "data", "knowledge", "observability", "utilities", "developer")
NETWORK = ("none", "internal")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::?-[^}]*)?\}")
CONFIG_NAME = re.compile(r"^MCP_[A-Z0-9_]+$")
SECRET_HINT = re.compile(r"PASS|PWD|TOKEN|SECRET|KEY|CREDENTIAL", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConfigItem:
    name: str
    description: str
    secret: bool
    required: bool


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    filename: str
    name: str
    display_name: str
    description: str
    category: str | None
    network: str | None
    writes: bool | None
    package: str | None
    package_kind: str | None
    source: str | None
    config: tuple[ConfigItem, ...]
    volumes: tuple[str, ...]
    suggested_risk: dict[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems

    @property
    def endpoint(self) -> str:
        """Derived, never declared: `make mcp-vendor` names the service ``mcp-<name>``."""
        return f"http://mcp-{self.name}:8000/"


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return {"true": True, "false": False}.get(text)


def validate(spec: dict[str, Any]) -> list[str]:
    """Every rule `scripts/vendor_mcp_servers.py` applies, in the same words."""
    problems: list[str] = []
    name = str(spec.get("name") or "")
    if not NAME_PATTERN.match(name):
        problems.append(f"name {name!r} must be lowercase letters, digits and hyphens")
    npm, pip = spec.get("npm"), spec.get("pip")
    if not npm and not pip:
        problems.append("needs either 'npm' or 'pip'")
    if npm and not NPM_PINNED.match(str(npm)):
        problems.append(f"npm package {npm!r} is not pinned to a version (use pkg@1.2.3)")
    if pip and not PIP_PINNED.match(str(pip)):
        problems.append(f"pip package {pip!r} is not pinned (use package==1.2.3)")
    if not spec.get("command"):
        problems.append("needs a 'command' to run the server")

    if spec.get("category") not in CATEGORIES:
        problems.append(f"category must be one of {', '.join(CATEGORIES)}")
    network = spec.get("network")
    if network == "external":
        problems.append("network 'external' is not allowed: connectors run air-gapped")
    elif network not in NETWORK:
        problems.append("network must be 'none' or 'internal'")
    if _as_bool(spec.get("writes", "")) is None:
        problems.append("writes must be true or false")

    config = spec.get("config") or {}
    if not isinstance(config, dict):
        problems.append("config must map .env variable names to descriptions")
        config = {}
    for key in config:
        if not CONFIG_NAME.match(str(key)):
            problems.append(f"config {key!r} must be an MCP_ variable in capitals")
    for list_field in ("secrets", "required"):
        listed = spec.get(list_field) or []
        if not isinstance(listed, list):
            problems.append(f"{list_field} must be a list")
            continue
        for key in listed:
            if key not in config:
                problems.append(f"{list_field} names {key!r}, which config does not describe")

    environment = spec.get("environment") or {}
    if isinstance(environment, dict):
        for key, raw in environment.items():
            value = str(raw)
            for ref in ENV_REF.findall(value):
                if ref not in config:
                    problems.append(
                        f"environment {key} reads ${{{ref}}}, which config does not describe"
                    )
            if SECRET_HINT.search(str(key)) and value and not ENV_REF.fullmatch(value):
                problems.append(
                    f"environment {key} looks secret; read it from .env with ${{MCP_...}}"
                )

    risks = spec.get("suggested_risk") or {}
    if not isinstance(risks, dict):
        problems.append("suggested_risk must map tool names to a risk level")
    else:
        for tool, level in risks.items():
            if level not in RISK_LEVELS:
                problems.append(f"suggested_risk {tool}: {level!r} is not {'/'.join(RISK_LEVELS)}")
    return problems


def parse(filename: str, spec: dict[str, Any]) -> ConnectorManifest:
    """A manifest with its problems listed; an invalid one is still shown, never installed."""
    config_spec = spec.get("config") if isinstance(spec.get("config"), dict) else {}
    secrets = set(spec.get("secrets") or []) if isinstance(spec.get("secrets"), list) else set()
    required = set(spec.get("required") or []) if isinstance(spec.get("required"), list) else set()
    risks = spec.get("suggested_risk")
    package_kind = "npm" if spec.get("npm") else "pip" if spec.get("pip") else None
    return ConnectorManifest(
        filename=filename,
        name=str(spec.get("name") or "").strip(),
        display_name=_text(spec.get("display_name") or spec.get("name")),
        description=_text(spec.get("description")),
        category=spec.get("category") if spec.get("category") in CATEGORIES else None,
        network=spec.get("network") if spec.get("network") in NETWORK else None,
        writes=_as_bool(spec.get("writes", "")),
        package=str(spec.get("npm") or spec.get("pip") or "") or None,
        package_kind=package_kind,
        source=_text(spec.get("source")) or None,
        config=tuple(
            ConfigItem(
                name=str(key),
                description=_text(text),
                secret=key in secrets,
                required=key in required,
            )
            for key, text in (config_spec or {}).items()
        ),
        volumes=tuple(str(v) for v in spec.get("volumes") or [] if isinstance(v, str)),
        suggested_risk={
            str(tool): str(level)
            for tool, level in (risks.items() if isinstance(risks, dict) else [])
            if level in RISK_LEVELS
        },
        problems=tuple(validate(spec)),
    )
