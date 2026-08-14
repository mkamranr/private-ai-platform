"""Authenticating to an external OpenAI-compatible endpoint (OpenRouter).

`VLLMProvider` was written for runtimes the platform starts itself, on a network only it
can reach, so it sent no credentials. Pointing it at a hosted endpoint adds exactly one
requirement — a bearer token — and these tests pin the two halves of that:

* the header is present when a key is configured, and **absent** when one is not, because
  every existing deployment resolves through the same provider and must not start sending
  an `Authorization: Bearer None` to a local vLLM;
* the key never appears in anything the platform writes down.
"""

from __future__ import annotations

from typing import Any

from app.config.settings import ModelsSettings
from app.services.llm_provider import ProviderError, VLLMProvider


async def test_no_authorization_header_without_a_key() -> None:
    """The default has to be byte-identical to the old behaviour.

    Every managed runtime — vllm, mock, sglang — constructs this provider with no key. A
    stray header here would be sent to every local engine on every request.
    """
    provider = VLLMProvider("http://mock-vllm:8000")
    async with provider._client() as client:
        assert "authorization" not in {k.lower() for k in client.headers}


async def test_authorization_header_when_a_key_is_configured() -> None:
    provider = VLLMProvider("https://openrouter.ai/api/v1", api_key="sk-or-test-123")
    async with provider._client() as client:
        assert client.headers["Authorization"] == "Bearer sk-or-test-123"


async def test_an_empty_key_is_treated_as_no_key() -> None:
    """`MODELS__EXTERNAL_API_KEY=` unset reads as an empty string, not as None.

    Sending `Bearer ` would turn "the operator has not configured this yet" into a 401
    from the provider, which is a far less obvious thing to debug than no header at all.
    """
    provider = VLLMProvider("https://openrouter.ai/api/v1", api_key="")
    async with provider._client() as client:
        assert "authorization" not in {k.lower() for k in client.headers}


class TestTheKeyGoesOnlyToItsOwnEndpoint:
    """`external_key_for` is the whole rule, shared by the gateway and the health probe.

    The runtime is too coarse a test on its own: `external` covers a hosted provider on
    the Internet *and* a llama.cpp container on this Docker network. Both are pointed at
    rather than started; only one issued the key.
    """

    def _settings(self, endpoint: str = "https://openrouter.ai/api", key: str = "sk-or-x") -> Any:
        from app.config.settings import ModelsSettings, Settings

        settings = Settings.model_construct()
        settings.models = ModelsSettings(  # type: ignore[call-arg]
            external_endpoint=endpoint,
            external_api_key=key,  # type: ignore[arg-type]
        )
        return settings

    def test_the_configured_endpoint_gets_the_key(self) -> None:
        from app.config.settings import external_key_for

        got = external_key_for(self._settings(), "external", "https://openrouter.ai/api")
        assert got == "sk-or-x"

    def test_a_trailing_slash_is_not_a_different_endpoint(self) -> None:
        from app.config.settings import external_key_for

        assert external_key_for(self._settings(), "external", "https://openrouter.ai/api/")

    def test_a_local_engine_registered_as_external_gets_nothing(self) -> None:
        """The case this rule exists for: llama.cpp on the platform network.

        It is `external` — the platform points at it rather than starting it — and posting
        a hosted provider's token to it would put that credential somewhere nobody meant.
        """
        from app.config.settings import external_key_for

        assert external_key_for(self._settings(), "external", "http://llamacpp:8080") is None

    def test_ollama_gets_nothing_even_at_a_matching_address(self) -> None:
        from app.config.settings import external_key_for

        assert external_key_for(self._settings(), "ollama", "https://openrouter.ai/api") is None

    def test_a_managed_runtime_gets_nothing(self) -> None:
        from app.config.settings import external_key_for

        for runtime in ("vllm", "mock", "llamacpp", "sglang"):
            assert external_key_for(self._settings(), runtime, "http://ai-model-abc:8000") is None

    def test_no_key_configured_means_no_header_anywhere(self) -> None:
        from app.config.settings import external_key_for

        settings = self._settings(key="")
        assert external_key_for(settings, "external", "https://openrouter.ai/api") is None


def test_the_key_is_not_in_the_settings_repr() -> None:
    """A key that reaches a log line or a traceback has leaked.

    `SecretStr` is what makes `/config`, structlog's event dict and an unhandled
    exception's frame locals safe to write down.
    """
    settings = ModelsSettings(external_api_key="sk-or-secret-value")  # type: ignore[arg-type]
    assert "sk-or-secret-value" not in repr(settings)
    assert "sk-or-secret-value" not in str(settings.external_api_key)
    # Still retrievable where it is actually needed.
    assert settings.external_api_key.get_secret_value() == "sk-or-secret-value"


def test_provider_errors_do_not_echo_the_key() -> None:
    """The provider quotes the response body on failure; it must not quote the request."""
    error = ProviderError("Inference runtime returned 401: unauthorized", status_code=401)
    assert "sk-or" not in str(error)
