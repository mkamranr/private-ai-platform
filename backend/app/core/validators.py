"""Custom pydantic validators.

Currently one: email validation that works on a closed network.
"""

from __future__ import annotations

from typing import Annotated

import email_validator
from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator

# An air-gapped site's users have internal addresses — admin@ai-platform.local,
# ops@corp.internal, and the .env.example default is admin@ai-platform.local.
# email_validator treats "local" and "localhost" as reserved and rejects them, which is
# correct for a public signup form and wrong here: it would make it impossible to
# create a user with the site's actual email address.
#
# Only those two are unreserved. "arpa", "invalid", "onion" and "test" stay rejected,
# because those genuinely are never a real mailbox.
_UNRESERVED_FOR_INTERNAL_USE = frozenset({"local", "localhost"})

email_validator.SPECIAL_USE_DOMAIN_NAMES = [
    name
    for name in email_validator.SPECIAL_USE_DOMAIN_NAMES
    if name not in _UNRESERVED_FOR_INTERNAL_USE
]


def validate_platform_email(value: str) -> str:
    """Validate an email address for syntax only, never deliverability.

    ``check_deliverability=False`` is a correctness requirement rather than a
    performance choice. It resolves MX records, and §25 states plainly that the
    platform must work when DNS is unavailable — so a deliverability check would
    hang until timeout and then reject a perfectly valid internal address.

    Returns the normalised form, so ``Admin@Example.COM`` and ``admin@example.com``
    cannot become two accounts.
    """
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError as exc:
        # Re-raised as ValueError so pydantic reports it as a field error.
        raise ValueError(str(exc)) from exc


#: Use in place of ``pydantic.EmailStr`` throughout the platform.
PlatformEmail = Annotated[str, AfterValidator(validate_platform_email)]
