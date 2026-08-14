"""The enrolment SSRF boundary (M04).

`validate_agent_url` decides whether the control plane will fetch an address a node
supplied. Most of this file is refusals, because the failure mode is not "a node cannot
enrol" — it is the control plane issuing requests onto its own management network on
behalf of whoever holds an enrolment token.

The DNS cases patch `socket.getaddrinfo`: resolution is the part that turns a harmless
hostname into a loopback connection, and it cannot be exercised without controlling it.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.config.settings import EnrollmentSettings
from app.core.agent_url import AgentUrlError, validate_agent_url


def resolving_to(*addresses: str):
    """Patch getaddrinfo so a hostname resolves to exactly what a test needs."""
    return patch(
        "app.core.agent_url.socket.getaddrinfo",
        return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in addresses],
    )


@pytest.fixture
def enroll() -> EnrollmentSettings:
    return EnrollmentSettings()


# -- refusals that never reach the resolver --------------------------------------------


@pytest.mark.parametrize(
    ("url", "because"),
    [
        ("", "empty"),
        ("   ", "blank"),
        ("gpu-node-01:9100", "no scheme"),
        ("file:///etc/passwd", "file scheme"),
        ("ftp://gpu-node-01:9100", "ftp scheme"),
        ("gopher://gpu-node-01:9100", "gopher scheme"),
        ("http://user:pass@gpu-node-01:9100", "credentials"),
        ("http://gpu-node-01:9100/latest/meta-data", "a path"),
        ("http://gpu-node-01:9100/?x=1", "a query"),
        ("http://gpu-node-01:9100/#frag", "a fragment"),
        ("http://:9100", "no host"),
        ("http://gpu-node-01:22", "a port outside the allowlist"),
        ("http://gpu-node-01:5432", "a port outside the allowlist"),
        ("http://gpu-node-01:80", "a port outside the allowlist"),
    ],
)
def test_refused_before_any_lookup(url: str, because: str, enroll: EnrollmentSettings) -> None:
    """None of these should cost a DNS query, let alone a connection."""
    with patch("app.core.agent_url.socket.getaddrinfo") as resolver, pytest.raises(AgentUrlError):
        validate_agent_url(url, enroll)
    assert not resolver.called, f"{because}: resolved {url!r} instead of refusing it outright"


# -- refusals that depend on what the name resolves to ---------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # the control plane calling itself
        "169.254.169.254",  # cloud metadata, the canonical SSRF target
        "0.0.0.0",
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
    ],
)
def test_refused_by_resolved_address(address: str, enroll: EnrollmentSettings) -> None:
    with resolving_to(address), pytest.raises(AgentUrlError):
        validate_agent_url("http://node.internal:9100", enroll)


def test_every_resolved_address_is_checked_not_just_the_first() -> None:
    """A name with one good record and one pointing at loopback must be refused.

    Checking only the first would pass validation and then connect to whichever the
    resolver happened to hand back next.
    """
    with resolving_to("10.0.4.21", "127.0.0.1"), pytest.raises(AgentUrlError):
        validate_agent_url("http://node.internal:9100", EnrollmentSettings())


def test_ipv6_loopback_is_refused() -> None:
    with (
        patch(
            "app.core.agent_url.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))],
        ),
        pytest.raises(AgentUrlError),
    ):
        validate_agent_url("http://node.internal:9100", EnrollmentSettings())


def test_unresolvable_host_is_refused(enroll: EnrollmentSettings) -> None:
    with (
        patch("app.core.agent_url.socket.getaddrinfo", side_effect=socket.gaierror),
        pytest.raises(AgentUrlError, match="could not be resolved"),
    ):
        validate_agent_url("http://nowhere.internal:9100", enroll)


# -- what must be accepted -------------------------------------------------------------


def test_private_addresses_are_allowed_by_design(enroll: EnrollmentSettings) -> None:
    """The inverse of public-internet SSRF advice, and deliberate.

    Every node on this platform is on a private network. Blocking RFC1918 here would
    reject every legitimate node.
    """
    with resolving_to("10.0.4.21"):
        assert validate_agent_url("http://gpu-node-01:9100", enroll).url == (
            "http://gpu-node-01:9100"
        )
    with resolving_to("192.168.7.5"):
        assert validate_agent_url("http://gpu-node-02:9100", enroll).host == "gpu-node-02"
    with resolving_to("172.16.3.9"):
        assert validate_agent_url("http://gpu-node-03:9100", enroll).port == 9100


def test_port_defaults_to_the_agent_port_not_eighty(enroll: EnrollmentSettings) -> None:
    with resolving_to("10.0.4.21"):
        result = validate_agent_url("http://gpu-node-01", enroll)
    assert result.port == 9100
    assert result.url == "http://gpu-node-01:9100"


def test_the_url_is_normalised(enroll: EnrollmentSettings) -> None:
    """Stored normalised, so two spellings of one node cannot look like two nodes."""
    with resolving_to("10.0.4.21"):
        result = validate_agent_url("HTTP://GPU-Node-01:9100/", enroll)
    assert result.url == "http://gpu-node-01:9100"


def test_resolved_addresses_are_reported_for_the_audit_trail(
    enroll: EnrollmentSettings,
) -> None:
    with resolving_to("10.0.4.21"):
        assert validate_agent_url("http://gpu-node-01:9100", enroll).addresses == ("10.0.4.21",)


# -- site policy knobs -----------------------------------------------------------------


def test_allowed_advertise_cidrs_pins_enrolment_to_one_subnet() -> None:
    """The control that makes a stolen token useless outside the building."""
    settings = EnrollmentSettings(allowed_advertise_cidrs=["10.0.4.0/24"])
    with resolving_to("10.0.4.21"):
        assert validate_agent_url("http://gpu-node-01:9100", settings).port == 9100
    with resolving_to("10.9.9.9"), pytest.raises(AgentUrlError, match="outside the address"):
        validate_agent_url("http://elsewhere:9100", settings)


def test_a_typo_in_the_cidr_allowlist_fails_closed() -> None:
    """It must not silently widen the allowlist to everything."""
    settings = EnrollmentSettings(allowed_advertise_cidrs=["10.0.4.0/33"])
    with resolving_to("10.0.4.21"), pytest.raises(AgentUrlError, match="not a valid CIDR"):
        validate_agent_url("http://gpu-node-01:9100", settings)


def test_require_https_refuses_plaintext() -> None:
    settings = EnrollmentSettings(require_https=True)
    with resolving_to("10.0.4.21"), pytest.raises(AgentUrlError, match="HTTPS"):
        validate_agent_url("http://gpu-node-01:9100", settings)
    with resolving_to("10.0.4.21"):
        assert validate_agent_url("https://gpu-node-01:9100", settings).url.startswith("https://")


def test_extra_allowed_ports_are_honoured() -> None:
    settings = EnrollmentSettings(allowed_agent_ports=[9100, 9443])
    with resolving_to("10.0.4.21"):
        assert validate_agent_url("https://gpu-node-01:9443", settings).port == 9443
