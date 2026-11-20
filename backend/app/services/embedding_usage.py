"""Embedding calls the platform makes for itself, accounted (expansion E21).

E19 recorded the platform's own chat calls (agent runs, workflow LLM steps), but not its
embeddings: ingesting a document, searching a knowledge base, an agent's or workflow's
retrieval, a RAG evaluation, and storing or recalling a memory all ran an embedding model
that no usage report, budget or chargeback saw. On a busy knowledge base that is most of
an embedding deployment's work.

Every such call now goes through :func:`embed_with`, the one route from a model alias to
vectors. It resolves the alias the way a `/v1/embeddings` request would, and after the
model answers writes one usage row through
:meth:`GatewayService.record_internal_usage`: the tokens the runtime reported, the
person and team in the :class:`EmbeddingCharge`, counted toward team and model token
budgets but never refused by them. A call without a charge is still recorded, with no
person or team, so nothing goes unseen.

A call that fails is not recorded: there are no tokens to report, and the caller's own
error handling says what happened.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.core.capabilities import Capability

if TYPE_CHECKING:
    from app.services.gateway import GatewayService

#: Endpoint recorded for an embedding nobody said the purpose of.
UNATTRIBUTED = "platform.embed"


@dataclass(frozen=True, slots=True)
class EmbeddingCharge:
    """Whom an embedding call is for, and why.

    ``endpoint`` says what the platform was doing (``knowledge.ingest``,
    ``knowledge.search``, ``memory.write``…). ``label`` is shown as the requested model
    on the Usage page, like E19's ``agent:<slug>``: ``kb:<name>`` or ``memory``.
    """

    endpoint: str
    label: str
    user_id: uuid.UUID | None = None
    team_id: uuid.UUID | None = None


class Embedder(Protocol):
    async def __call__(
        self, texts: list[str], model: str, *, charge: EmbeddingCharge | None = None
    ) -> list[list[float]]: ...


async def embed_with(
    gateway: GatewayService,
    texts: Sequence[str],
    model: str,
    charge: EmbeddingCharge | None = None,
) -> list[list[float]]:
    """Embed ``texts`` with ``model`` (an alias or model name) and account for it."""
    # E24: refused here, before the model is called, when budgets are enforced on the
    # platform's own calls and this one's team or model has used its budget.
    await gateway.check_internal_budget(model, charge.team_id if charge else None)
    started = time.perf_counter()
    # Served name, not the alias the caller passed: upstream has never heard of it.
    provider, served_model = await gateway.provider_for_model(model, needs=(Capability.EMBEDDINGS,))
    result = await provider.embeddings(list(texts), model=served_model)
    await gateway.record_internal_usage(
        endpoint=charge.endpoint if charge else UNATTRIBUTED,
        requested_model=charge.label if charge else model,
        configured_model=model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=0,
        user_id=charge.user_id if charge else None,
        team_id=charge.team_id if charge else None,
        started=started,
    )
    return [list(vector) for vector in result.vectors]


def embed_through(gateway: GatewayService) -> Embedder:
    """An embedding callable bound to one gateway: the same route a request would take."""

    async def embed(
        texts: list[str], model: str, *, charge: EmbeddingCharge | None = None
    ) -> list[list[float]]:
        return await embed_with(gateway, texts, model, charge)

    return embed
