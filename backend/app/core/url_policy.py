"""Outbound URL policy for addresses an administrator registers (§25, plan §5.17).

One policy, used by every place the platform is told "call this address": inference
backends, external model endpoints, and (next) MCP servers. Before this module each
registration path had its own idea of what was acceptable, and a rule that lives in
three places eventually holds in two of them.

What it refuses, and why:

* **Public AI SaaS hosts.** Registering one would send prompts and documents off the
  premises, which is the one thing this platform exists to prevent (Rule 4).
* **Cloud metadata endpoints.** The classic SSRF target: a "test connection" against
  ``169.254.169.254`` would hand an instance credential to whoever can register a URL.
* **Credentials in the URL.** ``http://user:pass@host`` ends up in logs, audit metadata
  and the UI. Secrets go in the dedicated encrypted field.
* **Anything that is not http(s).** ``file://`` and ``gopher://`` have no business here.

What it deliberately does **not** refuse: private and loopback addresses. An on-premises
platform's inference servers *are* on private addresses; refusing them would refuse the
product. The network is what enforces isolation; this is a guard against the honest
mistake and the obvious abuse, not a firewall.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from app.core.errors import ValidationError

#: Hosts that are, by definition, somebody else's computer. Matched on the registrable
#: suffix so a subdomain cannot slip past.
PUBLIC_AI_HOSTS: tuple[str, ...] = (
    "openai.com",
    "openai.azure.com",
    "anthropic.com",
    "elevenlabs.io",
    "googleapis.com",
    "google.com",
    "cognitiveservices.azure.com",
    "azure.com",
    "amazonaws.com",
    "deepgram.com",
    "assemblyai.com",
    "speechmatics.com",
    "huggingface.co",
)

#: Instance-metadata services. Names as well as addresses, because the name resolves to
#: the address only inside the cloud — exactly where the attack works.
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
        "metadata.google.internal",
        "metadata",
        "100.100.100.200",  # Alibaba Cloud
    }
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def public_ai_host(host: str) -> str | None:
    """Return the matched public suffix if ``host`` is a public AI service, else None."""
    lowered = host.lower().rstrip(".")
    for public in PUBLIC_AI_HOSTS:
        if lowered == public or lowered.endswith(f".{public}"):
            return public
    return None


def check_outbound_url(url: str, *, field: str = "base_url") -> str:
    """Validate an administrator-supplied URL and return it normalised.

    Normalised means: surrounding whitespace and trailing slashes removed, so
    ``http://h:8000/`` and ``http://h:8000`` are the same backend.

    Raises :class:`ValidationError` naming ``field`` so the UI can mark the input.
    """
    candidate = (url or "").strip()
    parsed = urlparse(candidate)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValidationError(
            f"{candidate!r} must start with http:// or https://.",
            details={"field": field},
        )
    host = parsed.hostname
    if not host:
        raise ValidationError(
            f"{candidate!r} has no host the platform can call.", details={"field": field}
        )
    if parsed.username or parsed.password:
        raise ValidationError(
            "Put credentials in the API key field, not in the URL — a URL is shown in the "
            "UI, written to logs and recorded in the audit trail.",
            details={"field": field},
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(
            f"{candidate!r} has an invalid port.", details={"field": field}
        ) from exc
    if port == 0:
        raise ValidationError(f"{candidate!r} has an invalid port.", details={"field": field})

    lowered = host.lower()
    if lowered in _METADATA_HOSTS or _is_link_local(lowered):
        raise ValidationError(
            f"{host} is a link-local or cloud metadata address, which is never an "
            "inference server and is the usual target of server-side request forgery.",
            details={"field": field, "host": host},
        )
    matched = public_ai_host(lowered)
    if matched:
        raise ValidationError(
            f"{host} is a public cloud service. This platform runs its models on the "
            "premises and is built to work with no Internet at all (Rule 4), so an "
            "endpoint must be a host inside your own network.",
            details={"field": field, "host": host},
        )
    return candidate.rstrip("/")


def _is_link_local(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_link_local
