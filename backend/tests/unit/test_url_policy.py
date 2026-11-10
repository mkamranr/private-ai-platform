"""The shared outbound URL policy (E1, plan §5.17)."""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.core.url_policy import check_outbound_url, public_ai_host


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://gpu-01:8000", "http://gpu-01:8000"),
        ("http://gpu-01:8000/", "http://gpu-01:8000"),
        ("  https://inference.corp.internal/  ", "https://inference.corp.internal"),
        # Private and loopback addresses are the product, not a threat.
        ("http://10.0.0.5:11434", "http://10.0.0.5:11434"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://host.docker.internal:11434", "http://host.docker.internal:11434"),
    ],
)
def test_accepts_on_premises_urls(url: str, expected: str) -> None:
    assert check_outbound_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gpu-01/models",
        "file:///etc/passwd",
        "gpu-01:8000",  # no scheme: urlparse reads 'gpu-01' as the scheme
        "http://",
        "",
    ],
)
def test_refuses_non_http_or_hostless(url: str) -> None:
    with pytest.raises(ValidationError):
        check_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://[fd00:ec2::254]/",
        "http://169.254.10.10:8000",
    ],
)
def test_refuses_metadata_and_link_local(url: str) -> None:
    with pytest.raises(ValidationError, match="metadata"):
        check_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://api.openai.com/v1", "https://foo.openai.azure.com", "https://api.anthropic.com"],
)
def test_refuses_public_ai_services(url: str) -> None:
    with pytest.raises(ValidationError, match="public cloud service"):
        check_outbound_url(url)


def test_refuses_credentials_in_url() -> None:
    with pytest.raises(ValidationError, match="API key field"):
        check_outbound_url("http://admin:hunter2@gpu-01:8000")


def test_error_names_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        check_outbound_url("ftp://x", field="endpoint_url")
    assert caught.value.details == {"field": "endpoint_url"}


def test_public_host_matches_suffix_not_substring() -> None:
    assert public_ai_host("api.openai.com") == "openai.com"
    # A host that merely contains the name is somebody's internal server.
    assert public_ai_host("openai.com.corp.internal") is None
    assert public_ai_host("notopenai.com") is None
