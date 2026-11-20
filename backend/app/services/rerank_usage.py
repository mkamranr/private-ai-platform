"""Reranking calls the platform makes for itself, accounted (expansion E38).

E21 put every embedding the platform runs on the books. Reranking stayed off them: a
knowledge base with a reranker scores dozens of passages for every search, agent or
workflow retrieval, playground query and evaluation case, and no usage report, budget or
chargeback saw any of it.

Every rerank now goes through :func:`rerank_with`, the one route from a model alias to
scores, the same way :func:`~app.services.embedding_usage.embed_with` does for vectors:
the alias is resolved as a request would resolve it, the team's and model's budgets are
checked first when the platform's own calls are enforced (E24), and one keyless usage row
is written after the model answers.

Tokens are what the engine reported. vLLM's ``/v1/rerank`` reports them; Hugging Face
TEI's ``/rerank`` does not, and then the row records the call with zero tokens rather
than an estimate. The call, its latency, its person and team are still there.

A call that fails is not recorded, as for embeddings: the retrieval falls back to vector
order and its trace says why.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

from app.core.capabilities import Capability
from app.core.logging import get_logger
from app.services.embedding_usage import EmbeddingCharge

if TYPE_CHECKING:
    from app.services.gateway import GatewayService

log = get_logger(__name__)

#: Endpoint recorded for a rerank nobody said the purpose of.
UNATTRIBUTED = "platform.rerank"

#: Rerank calls are charged like embeddings: what, for whom, under which label.
RerankCharge = EmbeddingCharge


class Reranker(Protocol):
    async def __call__(
        self,
        query: str,
        passages: list[str],
        model: str,
        *,
        charge: RerankCharge | None = None,
    ) -> list[float]: ...


async def rerank_with(
    gateway: GatewayService,
    query: str,
    passages: list[str],
    model: str,
    charge: RerankCharge | None = None,
) -> list[float]:
    """Score ``passages`` against ``query`` with ``model`` and account for it."""
    await gateway.check_internal_budget(model, charge.team_id if charge else None)
    started = time.perf_counter()
    provider, served_model = await gateway.provider_for_model(model, needs=(Capability.RERANK,))
    result = await provider.rerank_scored(query, passages, model=served_model)
    if result.usage is None:
        log.info("rerank_tokens_not_reported", model=model)
    await gateway.record_internal_usage(
        endpoint=charge.endpoint if charge else UNATTRIBUTED,
        requested_model=charge.label if charge else model,
        configured_model=model,
        prompt_tokens=result.usage.prompt_tokens if result.usage else 0,
        completion_tokens=0,
        user_id=charge.user_id if charge else None,
        team_id=charge.team_id if charge else None,
        started=started,
    )
    return list(result.scores)


def rerank_through(gateway: GatewayService) -> Reranker:
    """A reranking callable bound to one gateway: the route a request would take."""

    async def rerank(
        query: str, passages: list[str], model: str, *, charge: RerankCharge | None = None
    ) -> list[float]:
        return await rerank_with(gateway, query, passages, model, charge)

    return rerank
