"""Connector manifest rules (E10): the control plane and the build-machine vendor script
must accept and refuse exactly the same manifests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.services.connector_manifest import parse, validate

REPO = Path(__file__).resolve().parents[3]
MANIFESTS = sorted((REPO / "mcp" / "manifests").glob("*.yaml"))


def _vendor() -> Any:
    spec = importlib.util.spec_from_file_location(
        "vendor_mcp_servers", REPO / "scripts" / "vendor_mcp_servers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("vendor_mcp_servers", module)
    spec.loader.exec_module(module)
    return module


VENDOR = _vendor()

GOOD = """name: example
display_name: Example
description: >
  An example connector.
category: data
network: internal
writes: true
pip: "example-mcp==1.2.3"
command: example-mcp
environment:
  EXAMPLE_HOST: "${MCP_EXAMPLE_HOST:-db}"
  EXAMPLE_PASSWORD: "${MCP_EXAMPLE_PASSWORD:-}"
config:
  MCP_EXAMPLE_HOST: Host
  MCP_EXAMPLE_PASSWORD: Password
secrets:
  - MCP_EXAMPLE_PASSWORD
suggested_risk:
  read: LOW
  write: CRITICAL
"""

BAD = {
    "external network": GOOD.replace("network: internal", "network: external"),
    "unknown category": GOOD.replace("category: data", "category: games"),
    "unpinned": GOOD.replace('"example-mcp==1.2.3"', '"example-mcp"'),
    "literal secret": GOOD.replace('"${MCP_EXAMPLE_PASSWORD:-}"', '"hunter2-in-the-bundle"'),
    "undeclared variable": GOOD.replace("${MCP_EXAMPLE_HOST:-db}", "${MCP_OTHER:-db}"),
    "secret not in config": GOOD.replace(
        "  - MCP_EXAMPLE_PASSWORD", "  - MCP_EXAMPLE_PASSWORD\n  - MCP_NOWHERE"
    ),
    "bad risk": GOOD.replace("write: CRITICAL", "write: EXTREME"),
    "writes not a bool": GOOD.replace("writes: true", "writes: sometimes"),
}


def _both(tmp_path: Path, text: str) -> tuple[list[str], list[str]]:
    path = tmp_path / "example.yaml"
    path.write_text(text, encoding="utf-8")
    vendor_problems = VENDOR.validate(VENDOR._parse(path))
    backend_problems = validate(yaml.safe_load(text))
    return vendor_problems, backend_problems


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.name)
def test_every_shipped_manifest_passes_both(path: Path) -> None:
    assert VENDOR.validate(VENDOR._parse(path)) == []
    manifest = parse(path.name, yaml.safe_load(path.read_text(encoding="utf-8")))
    assert manifest.problems == ()
    assert manifest.network in ("none", "internal")
    assert manifest.category is not None


def test_catalogue_is_larger_than_the_reference_three() -> None:
    assert len(MANIFESTS) >= 7


def test_good_manifest_passes_both(tmp_path: Path) -> None:
    assert _both(tmp_path, GOOD) == ([], [])


@pytest.mark.parametrize("case", sorted(BAD))
def test_bad_manifest_is_refused_by_both_in_the_same_words(tmp_path: Path, case: str) -> None:
    vendor_problems, backend_problems = _both(tmp_path, BAD[case])
    assert backend_problems, case
    assert vendor_problems == backend_problems


def test_parse_marks_secrets_and_required(tmp_path: Path) -> None:
    manifest = parse("example.yaml", yaml.safe_load(GOOD))
    by_name = {c.name: c for c in manifest.config}
    assert by_name["MCP_EXAMPLE_PASSWORD"].secret is True
    assert by_name["MCP_EXAMPLE_HOST"].secret is False
    assert manifest.endpoint == "http://mcp-example:8000/"
    assert manifest.suggested_risk == {"read": "LOW", "write": "CRITICAL"}
    assert manifest.writes is True
