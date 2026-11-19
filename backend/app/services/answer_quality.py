"""An answer written from retrieved passages, and how faithful it is to them (E62).

Retrieval metrics (E5) say whether the right documents came back. They cannot say whether
the answer a model then wrote stayed inside them, and that is the failure people actually
report: a confident answer citing a policy that says something else. Two model calls close
the gap:

* **The answer** is written exactly as an agent would write it: the same context block
  (`services.retrieval.context_block_for`), so the playground shows what an agent with these
  knowledge bases would say, not a differently-prompted approximation.
* **The judge** splits that answer into claims and checks each against the numbered
  passages. Faithfulness is the share of claims the passages support. An answer that makes
  no claims ("the documents don't say") has no score rather than a perfect one, so a
  wall of refusals cannot read as 100%. Given a reference answer (an evaluation question's
  ``expected_answer``), the judge also says whether the answer agrees with it.

A judge is a model and can be wrong; its claims are returned so a person can check the
verdict, not just the number. Both calls go through the gateway like every platform call:
alias resolution, the team's and model's budgets, and a usage row.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.core.interfaces.llm import ChatMessage
from app.core.logging import get_logger
from app.services.knowledge import SearchResult
from app.services.retrieval import context_block_for

log = get_logger(__name__)

#: Longest answer asked for. A playground answer is read by a person checking retrieval.
ANSWER_MAX_TOKENS: Final = 700
#: The judge lists every claim; a long answer needs room.
JUDGE_MAX_TOKENS: Final = 1500
#: Claims kept from one verdict. More is a judge that has lost the thread.
MAX_CLAIMS: Final = 40
#: Characters of each passage the judge reads. The answer model reads the full passage.
JUDGE_PASSAGE_CHARS: Final = 3000

#: The reference-answer verdicts and their scores.
CORRECTNESS: Final[dict[str, float]] = {"agrees": 1.0, "partial": 0.5, "disagrees": 0.0}

ANSWER_SYSTEM: Final = (
    "You answer questions for the people of an organisation, from its documents. Be "
    "concise and specific."
)
NO_CONTEXT: Final = (
    "No passages were retrieved for this question. Say that the documents available to "
    "you do not cover it; do not answer from general knowledge."
)

JUDGE_SYSTEM: Final = """You check whether an ANSWER is supported by numbered source PASSAGES.

1. Split the ANSWER into its distinct factual claims, each short and self-contained.
2. For each claim decide whether the PASSAGES state it or directly imply it. A claim that
   may be true but is not in the PASSAGES is NOT supported. List the numbers of the
   passages that support it.
3. Saying the passages do not cover something, greetings, and questions back to the asker
   are not claims; leave them out. An answer with no claims has an empty list.

Reply with JSON only, no prose and no code fence:
{"claims": [{"claim": "...", "supported": true, "passages": [1]}]}"""

JUDGE_REFERENCE: Final = """

A REFERENCE ANSWER is also given. Add to the JSON:
"correctness": "agrees" | "partial" | "disagrees", and "correctness_reason": "<one sentence>",
judging whether the ANSWER gives the same facts as the REFERENCE ANSWER (wording may differ;
missing or contradicting facts make it partial or disagrees)."""


class JudgeError(RuntimeError):
    """The judge model answered, but not with a verdict that can be read."""


@dataclass(slots=True)
class GeneratedAnswer:
    text: str
    model: str
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(slots=True)
class Claim:
    claim: str
    supported: bool
    passages: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Verdict:
    judge_model: str
    claims: list[Claim] = field(default_factory=list)
    #: Supported over all claims; None when the answer makes no claims.
    faithfulness: float | None = None
    #: Against a reference answer: 1, 0.5 or 0; None when none was given.
    correctness: float | None = None
    correctness_label: str | None = None
    correctness_reason: str | None = None
    latency_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "judge_model": self.judge_model,
            "claims": [
                {"claim": c.claim, "supported": c.supported, "passages": c.passages}
                for c in self.claims
            ],
            "faithfulness": self.faithfulness,
            "supported": sum(1 for c in self.claims if c.supported),
            "unsupported": sum(1 for c in self.claims if not c.supported),
            "correctness": self.correctness,
            "correctness_label": self.correctness_label,
            "correctness_reason": self.correctness_reason,
            "latency_ms": round(self.latency_ms, 1),
        }


def _json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model's reply, tolerating a code fence or a preamble."""
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise JudgeError("The judge model did not reply with JSON.")
    try:
        value = json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise JudgeError("The judge model's reply is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise JudgeError("The judge model's reply is not a JSON object.")
    return value


def parse_verdict(
    text: str, *, judge_model: str, passages: int, reference: bool = False
) -> Verdict:
    """Read a judge's reply. Raises :class:`JudgeError` when it is not a verdict."""
    data = _json_object(text)
    raw = data.get("claims")
    if not isinstance(raw, list):
        raise JudgeError("The judge model's reply has no list of claims.")
    claims: list[Claim] = []
    for item in raw[:MAX_CLAIMS]:
        if not isinstance(item, dict) or not str(item.get("claim") or "").strip():
            continue
        cited = item.get("passages") or []
        numbers = sorted(
            {
                int(n)
                for n in (cited if isinstance(cited, list) else [cited])
                if isinstance(n, int | float | str)
                and str(n).strip().isdigit()
                and 1 <= int(n) <= passages
            }
        )
        supported = item.get("supported")
        if isinstance(supported, str):
            supported = supported.strip().lower() in {"true", "yes", "supported"}
        claims.append(
            Claim(
                claim=str(item["claim"]).strip()[:500], supported=bool(supported), passages=numbers
            )
        )
    verdict = Verdict(judge_model=judge_model, claims=claims)
    if claims:
        verdict.faithfulness = round(sum(1 for c in claims if c.supported) / len(claims), 4)
    if reference:
        label = str(data.get("correctness") or "").strip().lower()
        if label in CORRECTNESS:
            verdict.correctness_label = label
            verdict.correctness = CORRECTNESS[label]
            reason = data.get("correctness_reason")
            verdict.correctness_reason = str(reason).strip()[:500] if reason else None
    return verdict


def judge_prompt(
    question: str,
    answer: str,
    passages: Sequence[SearchResult],
    reference: str | None = None,
) -> list[ChatMessage]:
    numbered = "\n\n".join(
        f"[{i}] {p.document_name}{f', {p.location}' if p.location else ''}\n"
        f"{p.text.strip()[:JUDGE_PASSAGE_CHARS]}"
        for i, p in enumerate(passages, start=1)
    )
    body = (
        f"QUESTION:\n{question}\n\n"
        f"PASSAGES:\n{numbered or '(none were retrieved)'}\n\n"
        f"ANSWER:\n{answer}"
    )
    system = JUDGE_SYSTEM
    if reference:
        body += f"\n\nREFERENCE ANSWER:\n{reference}"
        system += JUDGE_REFERENCE
    return [ChatMessage(role="system", content=system), ChatMessage(role="user", content=body)]


def answer_prompt(question: str, passages: Sequence[SearchResult]) -> list[ChatMessage]:
    """The messages an agent with these passages would get, minus its own instructions."""
    context = context_block_for(passages) or NO_CONTEXT
    return [
        ChatMessage(role="system", content=ANSWER_SYSTEM),
        ChatMessage(role="system", content=context),
        ChatMessage(role="user", content=question),
    ]


ProviderFactory = Callable[[str], Awaitable[tuple[Any, str]]]


@dataclass(slots=True)
class Charge:
    """Who a model call is accounted to, and under what label."""

    user_id: uuid.UUID | None
    team_id: uuid.UUID | None
    label: str


class AnswerQualityService:
    """Writes answers and judges them, through the gateway's providers."""

    def __init__(
        self,
        provider_factory: ProviderFactory,
        *,
        usage: Callable[..., Awaitable[None]] | None = None,
        budget: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._provider_factory = provider_factory
        self._usage = usage
        self._budget = budget

    async def answer(
        self, question: str, passages: Sequence[SearchResult], *, model: str, charge: Charge
    ) -> GeneratedAnswer:
        completion, latency = await self._call(
            model,
            answer_prompt(question, passages),
            max_tokens=ANSWER_MAX_TOKENS,
            temperature=0.2,
            endpoint="retrieval.answer",
            charge=charge,
        )
        usage = completion.usage
        return GeneratedAnswer(
            text=(completion.content or "").strip(),
            model=model,
            latency_ms=latency,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )

    async def judge(
        self,
        question: str,
        answer: str,
        passages: Sequence[SearchResult],
        *,
        model: str,
        charge: Charge,
        reference: str | None = None,
    ) -> Verdict:
        """Raises :class:`JudgeError` for an unreadable verdict; model errors propagate."""
        completion, latency = await self._call(
            model,
            judge_prompt(question, answer, passages, reference),
            max_tokens=JUDGE_MAX_TOKENS,
            temperature=0.0,
            endpoint="retrieval.judge",
            charge=charge,
        )
        verdict = parse_verdict(
            completion.content or "",
            judge_model=model,
            passages=len(passages),
            reference=bool(reference),
        )
        verdict.latency_ms = latency
        return verdict

    async def _call(
        self,
        model: str,
        messages: list[ChatMessage],
        *,
        max_tokens: int,
        temperature: float,
        endpoint: str,
        charge: Charge,
    ) -> tuple[Any, float]:
        if self._budget is not None:
            await self._budget(model, charge.team_id)
        provider, served = await self._provider_factory(model)
        started = time.perf_counter()
        completion = await provider.chat(
            messages, model=served, temperature=temperature, max_tokens=max_tokens
        )
        latency = (time.perf_counter() - started) * 1000
        if self._usage is not None and completion.usage is not None:
            await self._usage(
                endpoint=endpoint,
                requested_model=charge.label[:128],
                configured_model=model,
                prompt_tokens=completion.usage.prompt_tokens,
                completion_tokens=completion.usage.completion_tokens,
                user_id=charge.user_id,
                team_id=charge.team_id,
                started=started,
            )
        return completion, latency
