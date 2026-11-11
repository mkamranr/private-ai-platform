"""Redaction for anything that leaves the platform in a support bundle (E7, plan §5.16).

The audit log's ``redact`` (``app/services/audit.py``) matches exact key names. That is
right for audit metadata, whose shapes this codebase controls. A diagnostics bundle is
different: it carries the effective configuration, log lines and error text, which hold
secrets in free text as well as under keys this code never chose. So this module is
deliberately broader and errs towards over-redaction:

* **keys**: any key whose name *contains* a secret-ish word (``password``, ``token``,
  ``secret``, ``key``, ``credential``, ``authorization``, ``cookie``, ``dsn``), with a
  short allowlist for keys that only look secret (``max_tokens``, ``key_fingerprint``);
* **values**: ``pydantic.SecretStr``/``SecretBytes`` always;
* **text**: credentials inside URLs, ``Bearer``/``Basic`` authorization values, JWTs,
  PEM private keys, the platform's own API-key format, and ``name=value`` pairs whose
  name is secret-ish.

Redaction replaces the value with ``[REDACTED]`` and keeps the structure, so a support
engineer still sees *that* a setting is set and where a URL points.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import SecretBytes, SecretStr

REDACTED = "[REDACTED]"

_SECRET_WORDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "private_key",
    "credential",
    "authorization",
    "cookie",
    "encryption_key",
    "signing_key",
    "access_key",
    "dsn",
)

#: Keys that contain a secret-ish word but hold nothing sensitive. Matched exactly
#: (lower-cased) so the allowlist cannot accidentally widen.
_SAFE_KEYS = frozenset(
    {
        "max_tokens",
        "max_output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "tokens",
        "token_budget",
        "key_fingerprint",
        "encryption_key_fingerprint",
        "redact_headers",
        "token_ttl_minutes",
        "access_token_ttl_minutes",
        "refresh_token_ttl_days",
        "token_type",
        "has_token",
        "has_secret",
        "secret_set",
    }
)

_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^\s:/@]*):(?P<pw>[^\s@/]+)@"
)
_AUTH_HEADER = re.compile(
    r"(?i)\b(?P<kind>bearer|basic|token)\s+(?P<value>[A-Za-z0-9\-._~+/]{8,}=*)"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b")
_PEM = re.compile(r"-----BEGIN (?P<label>[A-Z ]*PRIVATE KEY)-----.*?-----END (?P=label)-----", re.S)
#: The platform's API keys (``aip_…``) and common vendor-style keys (``sk-…``).
_API_KEY = re.compile(r"\b(?:aip|sk|pk|rk)[-_][A-Za-z0-9_\-]{16,}\b")
_ASSIGNMENT = re.compile(
    r"(?i)(?P<name>\b[A-Za-z0-9_.\-]*(?:"
    + "|".join(re.escape(w) for w in _SECRET_WORDS)
    + r")[A-Za-z0-9_.\-]*)(?P<sep>\s*[=:]\s*)(?P<quote>[\"']?)(?P<value>[^\s\"',;&]+)"
)


def is_secret_key(name: str) -> bool:
    lowered = name.lower().replace("-", "_")
    if lowered in _SAFE_KEYS:
        return False
    return any(word in lowered for word in _SECRET_WORDS)


def redact_text(text: str) -> str:
    """Scrub secrets embedded in free text: log lines, error messages, URLs."""
    if not text:
        return text
    text = _PEM.sub(
        lambda m: f"-----BEGIN {m['label']}-----{REDACTED}-----END {m['label']}-----", text
    )
    text = _URL_CREDENTIALS.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", text)
    text = _JWT.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: f"{m['kind']} {REDACTED}", text)
    text = _API_KEY.sub(REDACTED, text)

    def assignment(m: re.Match[str]) -> str:
        if m["name"].lower().replace("-", "_") in _SAFE_KEYS or m["value"] == REDACTED:
            return m[0]
        return f"{m['name']}{m['sep']}{m['quote']}{REDACTED}"

    return _ASSIGNMENT.sub(assignment, text)


def redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact a structure: secret keys, secret types and secrets in strings."""
    if isinstance(value, (SecretStr, SecretBytes)):
        return REDACTED
    if _depth > 12:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if is_secret_key(name) and item not in (None, "", [], {}):
                out[name] = REDACTED
            else:
                out[name] = redact_value(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return REDACTED
    return value
