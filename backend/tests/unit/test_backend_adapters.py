"""Inference backend adapters against a mock transport (E1, plan §5.2).

No server and no network: each test hands the adapter an ``httpx.MockTransport`` that
answers like the real backend would, including the ways real backends fail.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import ValidationError
from app.core.interfaces.inference_backend import BackendCapability
from app.services.inference_backends import (
    ADAPTERS,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    VllmAdapter,
    normalise_base_url,
    platform_model_name,
)


def _transport(routes: dict[str, httpx.Response], seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        response = routes.get(request.url.path)
        return response if response is not None else httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_vllm_reports_models_and_version() -> None:
    adapter = VllmAdapter(
        "http://vllm:8000",
        transport=_transport(
            {
                "/v1/models": httpx.Response(
                    200, json={"data": [{"id": "Qwen/Qwen2.5-7B", "owned_by": "vllm"}]}
                ),
                "/version": httpx.Response(200, json={"version": "0.11.0"}),
            }
        ),
    )
    result = await adapter.test_connection()
    assert result.ok
    assert result.model_count == 1
    assert result.version == "0.11.0"
    assert result.latency_ms is not None
    models = await adapter.list_models()
    assert [m.id for m in models] == ["Qwen/Qwen2.5-7B"]


async def test_missing_version_endpoint_does_not_fail_the_test() -> None:
    adapter = VllmAdapter(
        "http://vllm:8000",
        transport=_transport({"/v1/models": httpx.Response(200, json={"data": []})}),
    )
    result = await adapter.test_connection()
    assert result.ok
    assert result.version is None
    assert result.model_count == 0


async def test_ollama_parses_tags() -> None:
    adapter = OllamaAdapter(
        "http://host.docker.internal:11434",
        transport=_transport(
            {
                "/api/tags": httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "llama3.2:latest",
                                "size": 2019393189,
                                "details": {"family": "llama", "quantization_level": "Q4_K_M"},
                            }
                        ]
                    },
                ),
                "/api/version": httpx.Response(200, json={"version": "0.5.7"}),
            }
        ),
    )
    result = await adapter.test_connection()
    assert result.ok and result.version == "0.5.7" and result.model_count == 1
    (model,) = await adapter.list_models()
    assert model.id == "llama3.2:latest"
    assert model.details["family"] == "llama"
    assert model.details["size_bytes"] == 2019393189


async def test_api_key_is_sent_as_bearer() -> None:
    seen: list[httpx.Request] = []
    adapter = OpenAICompatibleAdapter(
        "http://gw:4000",
        api_key="sk-local-123",
        transport=_transport({"/v1/models": httpx.Response(200, json={"data": []})}, seen),
    )
    await adapter.test_connection()
    assert seen[0].headers["authorization"] == "Bearer sk-local-123"


async def test_no_key_means_no_authorization_header() -> None:
    seen: list[httpx.Request] = []
    adapter = OpenAICompatibleAdapter(
        "http://gw:4000",
        transport=_transport({"/v1/models": httpx.Response(200, json={"data": []})}, seen),
    )
    await adapter.test_connection()
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("response", "fragment"),
    [
        (httpx.Response(401), "rejected the credentials"),
        (httpx.Response(404), "not the API the backend type expects"),
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(200, text="<html>login</html>"), "not with the JSON"),
        (httpx.Response(200, json={"models": []}), "not with the JSON"),
    ],
)
async def test_failures_are_returned_not_raised(response: httpx.Response, fragment: str) -> None:
    adapter = VllmAdapter("http://vllm:8000", transport=_transport({"/v1/models": response}))
    result = await adapter.test_connection()
    assert not result.ok
    assert fragment in (result.error or "")


async def test_connection_error_explains_localhost() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = OllamaAdapter("http://localhost:11434", transport=httpx.MockTransport(refuse))
    result = await adapter.test_connection()
    assert not result.ok
    assert "host.docker.internal" in (result.error or "")


async def test_error_message_never_contains_the_key() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = VllmAdapter(
        "http://vllm:8000", api_key="sk-secret-value", transport=httpx.MockTransport(refuse)
    )
    result = await adapter.test_connection()
    assert "sk-secret-value" not in (result.error or "")


async def test_redirects_are_not_followed() -> None:
    adapter = VllmAdapter(
        "http://vllm:8000",
        transport=_transport(
            {"/v1/models": httpx.Response(302, headers={"location": "http://169.254.169.254/"})}
        ),
    )
    result = await adapter.test_connection()
    assert not result.ok


def test_no_adapter_claims_lifecycle_control() -> None:
    """Plan §5.2: do not pretend a backend can deploy or stop models."""
    for adapter in ADAPTERS.values():
        assert BackendCapability.DEPLOY not in adapter.capabilities
        assert BackendCapability.STOP not in adapter.capabilities
        assert BackendCapability.LIST_MODELS in adapter.capabilities


@pytest.mark.parametrize(
    ("url", "backend_type", "expected"),
    [
        ("http://vllm:8000/v1", "vllm", "http://vllm:8000"),
        ("http://vllm:8000/v1/", "openai_compatible", "http://vllm:8000"),
        ("http://ollama:11434", "ollama", "http://ollama:11434"),
        ("http://gw:4000/api/v1", "openai_compatible", "http://gw:4000/api"),
    ],
)
def test_normalise_base_url(url: str, backend_type: str, expected: str) -> None:
    assert normalise_base_url(url, backend_type) == expected


def test_normalise_applies_url_policy() -> None:
    with pytest.raises(ValidationError):
        normalise_base_url("http://169.254.169.254", "vllm")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("llama3.2:latest", "llama3-2-latest"),
        ("Qwen/Qwen2.5-7B-Instruct", "qwen-qwen2-5-7b-instruct"),
    ],
)
def test_platform_model_name(raw: str, expected: str) -> None:
    assert platform_model_name(raw) == expected
