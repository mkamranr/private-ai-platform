"""API key scopes (M20, Phase 6).

The property that matters most here is the one nobody asks for: **an empty scope list
means unrestricted**. Every key minted before scopes existed has one, and reading it as
"may do nothing" would take every running integration offline the moment the platform was
upgraded — a total outage caused by a feature nobody opted into.
"""

from __future__ import annotations

import pytest

from app.core.errors import PermissionDeniedError
from app.models.models_registry import ApiKey
from app.services.gateway import GatewayService


def _key(*scopes: str) -> ApiKey:
    return ApiKey(name="t", prefix="abc", key_hash="x", scopes=list(scopes))


def test_no_scopes_is_unrestricted() -> None:
    """The upgrade-safety property. A key from before scopes existed keeps working."""
    key = _key()
    for surface in ("chat", "embeddings", "models"):
        GatewayService.check_scope(key, surface=surface, alias="anything")


def test_a_surface_scope_refuses_other_surfaces() -> None:
    key = _key("chat")
    GatewayService.check_scope(key, surface="chat", alias="enterprise-chat")

    with pytest.raises(PermissionDeniedError, match="not scoped for 'embeddings'"):
        GatewayService.check_scope(key, surface="embeddings", alias="enterprise-chat")


def test_a_model_scope_refuses_other_models() -> None:
    key = _key("model:enterprise-chat")
    GatewayService.check_scope(key, surface="chat", alias="enterprise-chat")

    with pytest.raises(PermissionDeniedError, match="not scoped for the model"):
        GatewayService.check_scope(key, surface="chat", alias="enterprise-fast")


def test_the_two_dimensions_are_independent() -> None:
    """A key scoped only to surfaces may call any model, and vice versa.

    Restricting a dimension is opt-in. If naming one surface implicitly restricted models
    to none, a key scoped `["chat"]` would be unusable — and the operator who wrote it
    would have no way to see why from the scope list.
    """
    surfaces_only = _key("chat")
    GatewayService.check_scope(surfaces_only, surface="chat", alias="any-model-at-all")

    models_only = _key("model:enterprise-chat")
    GatewayService.check_scope(models_only, surface="embeddings", alias="enterprise-chat")


def test_both_dimensions_are_enforced_together() -> None:
    key = _key("chat", "model:enterprise-chat")
    GatewayService.check_scope(key, surface="chat", alias="enterprise-chat")

    with pytest.raises(PermissionDeniedError):
        GatewayService.check_scope(key, surface="embeddings", alias="enterprise-chat")
    with pytest.raises(PermissionDeniedError):
        GatewayService.check_scope(key, surface="chat", alias="something-else")


def test_the_refusal_names_what_the_key_may_use() -> None:
    """The caller holds a valid credential and asked a reasonable question. A bare denial
    sends them to an administrator for information the platform already has."""
    key = _key("model:enterprise-chat", "model:enterprise-fast")

    with pytest.raises(PermissionDeniedError) as raised:
        GatewayService.check_scope(key, surface="chat", alias="secret-model")

    message = str(raised.value)
    assert "enterprise-chat" in message
    assert "enterprise-fast" in message


def test_no_key_is_not_an_error() -> None:
    """An internal caller never went through gateway auth. Scoping is a property of a
    credential, so with no credential there is nothing to check — and raising here would
    break every internal call path instead."""
    GatewayService.check_scope(None, surface="chat", alias="anything")
