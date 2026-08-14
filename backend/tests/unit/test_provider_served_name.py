"""An alias must never be sent upstream (§13).

The platform's names for a model — `enterprise-chat`, or a sanitised
`nvidia-nemotron-3-ultra-550b-a55b-free` — are meaningless to the server actually holding
the weights. `ResolvedTarget.served_model_name` is the translation, and the gateway's own
chat paths have always used it.

`provider_for_model` did not: it returned only the provider, so the agent runtime and all
three embedding helpers sent on the name they already had. Nothing caught it because
`mock-vllm` answers to any model name, which is exactly the property that makes it useful
and exactly the property that hid this. Against a real hosted provider the run fails with
`400: enterprise-chat is not a valid model ID`.

These tests pin the contract rather than the plumbing: whatever `resolve` decides the
served name is, that is what the caller must be handed.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.gateway import GatewayService, ResolvedTarget


class _StubModel:
    """Only what `_provider` reads: the runtime and the endpoint decide the credential.

    `endpoint_url` is set because the gateway consults it — a stub that omits a field the
    code under test reads fails with an `AttributeError` that says nothing about the
    behaviour being tested, which is exactly what happened when the credential rule moved
    from being runtime-based to endpoint-scoped.
    """

    def __init__(self, runtime: str, endpoint_url: str | None = None) -> None:
        self.runtime = runtime
        self.endpoint_url = endpoint_url
        self.id = uuid.uuid4()


def _gateway(settings: Any, target: ResolvedTarget) -> GatewayService:
    """A gateway whose `resolve` is fixed, so no database is needed to test the contract."""
    gateway = GatewayService.__new__(GatewayService)
    gateway._settings = settings  # type: ignore[attr-defined]

    async def resolve(requested: str) -> ResolvedTarget:
        return target

    gateway.resolve = resolve  # type: ignore[method-assign]
    return gateway


def _target(
    runtime: str, served: str, endpoint: str = "https://openrouter.ai/api"
) -> ResolvedTarget:
    return ResolvedTarget(
        requested_model="enterprise-chat",
        model=_StubModel(runtime, endpoint),  # type: ignore[arg-type]
        deployment=object(),  # type: ignore[arg-type]
        internal_url=endpoint,
        served_model_name=served,
    )


async def test_provider_for_model_hands_back_the_served_name() -> None:
    """The alias went in; the upstream name must come out."""
    from app.config.settings import get_settings

    settings = get_settings()
    target = _target("external", "nvidia/nemotron-3-ultra-550b-a55b:free")

    provider, served = await _gateway(settings, target).provider_for_model("enterprise-chat")

    assert served == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert served != "enterprise-chat", "the alias must not be what callers send upstream"
    assert provider is not None


async def test_a_managed_runtime_still_resolves_its_own_name() -> None:
    """For a container the platform started, the two names agree — and must keep agreeing."""
    from app.config.settings import get_settings

    target = _target("mock", "mock-model")
    _, served = await _gateway(get_settings(), target).provider_for_model("enterprise-chat")
    assert served == "mock-model"
