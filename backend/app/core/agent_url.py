"""Validate an address a node claims to be reachable at (M04).

**This is an SSRF boundary.** During enrolment a node tells the control plane where to
find it, and the control plane then fetches that URL. The caller must hold a live
enrolment token, so it is not an open redirector — but a token holder who supplies a
hostile address would otherwise turn the control plane into a request proxy onto its own
management network. Everything below runs *before* a socket is opened.

**Private address ranges are allowed, on purpose.** The usual public-internet advice is to
reject RFC1918, and following it here would block the product: this platform is air-gapped
and every node is on a private network. The controls that do the work instead are the
**port allowlist** — a node agent has no business on 22 or 5432 — and, for a site that
wants it, ``allowed_advertise_cidrs`` pinned to the GPU subnet. Do not "harden" this module
by blocking private ranges; that would make it reject every legitimate node.

Known residual: this resolves the host here, and httpx resolves again when it connects, so
a DNS rebind between the two is not prevented. Pinning the connection to the resolved
address needs a custom transport and is deliberately left for a later pass. What bounds it
meanwhile is that only a valid-token holder can trigger a fetch at all, the port is pinned,
redirects are refused by the client, and the attempt count is capped per token in Postgres.
The resolved address is recorded on the enrolment row so the discrepancy is at least
visible afterwards.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config.settings import EnrollmentSettings


class AgentUrlError(ValueError):
    """The advertised address was refused. The message is safe to return to the caller."""


@dataclass(frozen=True)
class ValidatedAgentUrl:
    url: str
    """Normalised: lowercase host, explicit port, no trailing slash."""
    host: str
    port: int
    addresses: tuple[str, ...]
    """Every address the host resolved to, all of which passed the checks."""


def validate_agent_url(raw: str, settings: EnrollmentSettings) -> ValidatedAgentUrl:
    """Normalise and vet an agent address, or raise :class:`AgentUrlError`."""
    candidate = (raw or "").strip()
    if not candidate:
        raise AgentUrlError("No agent address was given.")
    if len(candidate) > 512:
        raise AgentUrlError("The agent address is too long.")

    parts = urlsplit(candidate)

    if parts.scheme not in ("http", "https"):
        raise AgentUrlError("The agent address must start with http:// or https://.")
    if settings.require_https and parts.scheme != "https":
        raise AgentUrlError("This platform requires node agents to be reached over HTTPS.")

    # `https://user:pass@real-host@evil-host/` is read differently by different parsers,
    # and credentials in a URL end up in logs. Never legitimate for an agent.
    if parts.username or parts.password:
        raise AgentUrlError("The agent address must not contain credentials.")

    # The client appends `/health` and the other agent paths itself. Anything here is
    # either a mistake or an attempt at path confusion.
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise AgentUrlError("The agent address must be a bare host and port, with no path.")

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:  # malformed port, e.g. `host:99999`
        raise AgentUrlError("The agent address has an invalid port.") from exc
    if not hostname:
        raise AgentUrlError("The agent address has no host.")

    # Default to the agent's own port rather than 80/443, then pin it. This is the control
    # that most reduces what a hostile address could reach.
    port = port or settings.default_agent_port
    if port not in settings.allowed_agent_ports:
        allowed = ", ".join(str(p) for p in settings.allowed_agent_ports)
        raise AgentUrlError(f"Port {port} is not allowed for a node agent. Allowed: {allowed}.")

    addresses = _resolve(hostname)
    permitted = _cidrs(settings.allowed_advertise_cidrs)
    for address in addresses:
        _reject_unroutable(address, hostname)
        if permitted and not any(address in network for network in permitted):
            raise AgentUrlError(
                f"{hostname} resolves to {address}, which is outside the address ranges "
                "this platform accepts nodes from."
            )

    host_part = f"[{hostname}]" if ":" in hostname else hostname
    return ValidatedAgentUrl(
        url=f"{parts.scheme}://{host_part.lower()}:{port}",
        host=hostname.lower(),
        port=port,
        addresses=tuple(str(a) for a in addresses),
    )


def _resolve(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address the host resolves to. **All** of them are checked, not just the first.

    A hostname with one legitimate A record and one pointing at loopback would otherwise
    pass validation and then connect to whichever the resolver returned second.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AgentUrlError(f"{hostname} could not be resolved from the control plane.") from exc

    addresses = []
    seen = set()
    for info in infos:
        literal = info[4][0]
        if literal in seen:
            continue
        seen.add(literal)
        try:
            addresses.append(ipaddress.ip_address(literal))
        except ValueError:  # pragma: no cover — getaddrinfo output
            continue
    if not addresses:
        raise AgentUrlError(f"{hostname} could not be resolved from the control plane.")
    return addresses


def _reject_unroutable(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, hostname: str
) -> None:
    """Refuse addresses that are not a node, whatever the caller claims.

    Loopback is the control plane talking to itself. Link-local covers cloud metadata at
    169.254.169.254, which is the canonical SSRF target. The rest cannot be a host.
    """
    if address.is_loopback:
        raise AgentUrlError(f"{hostname} resolves to {address}, which is the loopback address.")
    if address.is_link_local:
        raise AgentUrlError(f"{hostname} resolves to {address}, which is a link-local address.")
    if address.is_unspecified:
        raise AgentUrlError(f"{hostname} resolves to {address}, which is not a host address.")
    if address.is_multicast or address.is_reserved:
        raise AgentUrlError(f"{hostname} resolves to {address}, which is not a host address.")


def _cidrs(values: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            # A typo in configuration must not silently widen the allowlist to everything.
            raise AgentUrlError(
                f"Configured advertise range {value!r} is not a valid CIDR."
            ) from exc
    return networks
