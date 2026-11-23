"""Answers, faithfulness and the playground's question history (E62).

Retrieval runs for real over the E5 test corpus (`test_rag_depth`); the answer and judge
models are a scripted provider, so a test controls what each "says" and checks what the
platform does with it: the claims counted, failures reported beside the results, history
kept and withheld, replays compared and evaluation runs scored.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from app.api.deps import get_answer_quality_service, get_playground_service
from app.core.errors import DependencyUnavailableError, RateLimitError
from app.core.interfaces.llm import ChatCompletion, TokenUsage
from app.core.permissions import Permission as Perm
from app.models.knowledge import RetrievalQuery
from app.repositories.knowledge import KnowledgeBaseRepository, RetrievalQueryRepository
from app.services.answer_quality import (
    AnswerQualityService,
    JudgeError,
    answer_prompt,
    parse_verdict,
)
from app.services.knowledge import SearchResult
from app.services.retrieval_playground import PlaygroundService
from tests.api.conftest import _user_with
from tests.api.test_rag_depth import add_document, corpus, rag  # noqa: F401
from tests.conftest import auth_header

JUDGE_TWO_CLAIMS = json.dumps(
    {
        "claims": [
            {"claim": "Reset it in the self service portal.", "supported": True, "passages": [1]},
            {"claim": "It takes two minutes.", "supported": False, "passages": []},
        ]
    }
)


class ScriptedProvider:
    """Answers as the answer model or the judge, depending on the system prompt."""

    def __init__(self) -> None:
        self.answer = "Use the self service portal. It takes two minutes."
        self.judge = JUDGE_TWO_CLAIMS
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        judging = messages[0].content.startswith("You check whether")
        self.calls.append({"judge": judging, "messages": messages, **kwargs})
        return ChatCompletion(
            id="c",
            model=kwargs.get("model", "m"),
            content=self.judge if judging else self.answer,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25),
        )


@pytest.fixture
def models(app) -> SimpleNamespace:
    provider = ScriptedProvider()
    usage: list[dict[str, Any]] = []
    refused: set[str] = set()

    async def factory(model: str, **_: Any):
        if model == "not-served":
            raise DependencyUnavailableError("Nothing is serving not-served.")
        return provider, f"served-{model}"

    async def record(**kwargs: Any) -> None:
        usage.append(kwargs)

    async def budget(model: str, team_id: Any) -> None:
        if model in refused:
            raise RateLimitError(f"The token budget for {model} is used up.")

    service = AnswerQualityService(factory, usage=record, budget=budget)
    app.dependency_overrides[get_answer_quality_service] = lambda: service
    yield SimpleNamespace(provider=provider, usage=usage, refused=refused)
    app.dependency_overrides.pop(get_answer_quality_service, None)


async def _ask(client, headers, base, **extra: Any):
    return await client.post(
        "/api/v1/retrieval/playground",
        headers=headers,
        json={
            "knowledge_base_ids": [str(base.id)],
            "query": "reset password",
            "top_k": 2,
            "reranker": "none",
            **extra,
        },
    )


# ---------------------------------------------------------------------------
# Reading a verdict (pure)
# ---------------------------------------------------------------------------
class TestParseVerdict:
    def test_counts_supported_claims(self) -> None:
        verdict = parse_verdict(JUDGE_TWO_CLAIMS, judge_model="j", passages=2)
        assert verdict.faithfulness == 0.5
        assert [c.supported for c in verdict.claims] == [True, False]
        assert verdict.claims[0].passages == [1]

    def test_tolerates_a_code_fence_and_drops_unknown_passages(self) -> None:
        claims = '{"claims": [{"claim": "x", "supported": "yes", "passages": [1, 7, "2"]}]}'
        reply = f"Here you go:\n```json\n{claims}\n```"
        verdict = parse_verdict(reply, judge_model="j", passages=2)
        assert verdict.faithfulness == 1.0
        assert verdict.claims[0].passages == [1, 2]

    def test_no_claims_has_no_score_rather_than_a_perfect_one(self) -> None:
        verdict = parse_verdict('{"claims": []}', judge_model="j", passages=3)
        assert verdict.faithfulness is None and verdict.claims == []

    @pytest.mark.parametrize("reply", ["not json at all", '{"verdict": "fine"}', "[1, 2]"])
    def test_an_unreadable_reply_raises(self, reply: str) -> None:
        with pytest.raises(JudgeError):
            parse_verdict(reply, judge_model="j", passages=1)

    def test_reads_correctness_against_a_reference(self) -> None:
        reply = json.dumps(
            {
                "claims": [{"claim": "a", "supported": True, "passages": [1]}],
                "correctness": "partial",
                "correctness_reason": "Misses the second step.",
            }
        )
        verdict = parse_verdict(reply, judge_model="j", passages=1, reference=True)
        assert (verdict.correctness, verdict.correctness_label) == (0.5, "partial")
        assert verdict.correctness_reason == "Misses the second step."
        # Without a reference the field is ignored even if the judge volunteers it.
        assert parse_verdict(reply, judge_model="j", passages=1).correctness is None

    def test_the_answer_reads_what_an_agent_reads(self) -> None:
        passage = SearchResult(
            text="Reset via the portal.",
            score=0.9,
            document_id=uuid.uuid4(),
            document_name="howto.txt",
            knowledge_base="kb",
        )
        messages = answer_prompt("how?", [passage])
        assert "## Retrieved context" in messages[1].content
        assert "[1] howto.txt" in messages[1].content
        assert "do not cover it" in answer_prompt("how?", [])[1].content


# ---------------------------------------------------------------------------
# The playground
# ---------------------------------------------------------------------------
class TestPlaygroundAnswers:
    async def test_answers_judges_and_saves(self, client, tokens, rag, superuser, models) -> None:
        base, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        response = await _ask(client, headers, base, answer_model="chat", judge_model="judge")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["answer"]["text"].startswith("Use the self service portal")
        assert body["answer"]["model"] == "chat"
        verdict = body["verdict"]
        assert verdict["faithfulness"] == 0.5
        assert (verdict["supported"], verdict["unsupported"]) == (1, 1)
        assert body["history_id"]

        # The answer model was given the passages retrieved, numbered as an agent sees them.
        answer_call = next(c for c in models.provider.calls if not c["judge"])
        assert answer_call["model"] == "served-chat"
        assert "howto.txt" in answer_call["messages"][1].content
        # Both calls accounted, to the base, under their own endpoint.
        assert {u["endpoint"] for u in models.usage} == {"retrieval.answer", "retrieval.judge"}
        assert all(u["requested_model"] == f"kb:{base.name}" for u in models.usage)
        assert all(u["user_id"] == superuser.id for u in models.usage)

        saved = await client.get(f"/api/v1/retrieval/history/{body['history_id']}", headers=headers)
        assert saved.status_code == 200
        detail = saved.json()
        assert detail["mine"] is True and detail["faithfulness"] == 0.5
        assert detail["answer"].startswith("Use the self service portal")
        assert detail["results"][0]["text"]
        assert detail["settings"]["judge_model"] == "judge"
        assert len(detail["verdict"]["claims"]) == 2

    async def test_a_model_not_serving_is_reported_beside_the_results(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        response = await _ask(
            client, auth_header(tokens, superuser), base, answer_model="not-served"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["run"]["results"]
        assert body["answer"] is None
        assert "not-served" in body["answer_error"]

    async def test_a_used_up_budget_is_reported(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        models.refused.add("judge")
        body = (
            await _ask(
                client,
                auth_header(tokens, superuser),
                base,
                answer_model="chat",
                judge_model="judge",
            )
        ).json()
        assert body["answer"]["text"]
        assert body["verdict"] is None and "budget" in body["judge_error"]

    async def test_an_unreadable_verdict_is_reported(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        models.provider.judge = "I think it is mostly fine."
        body = (
            await _ask(
                client,
                auth_header(tokens, superuser),
                base,
                answer_model="chat",
                judge_model="judge",
            )
        ).json()
        assert body["verdict"] is None and "JSON" in body["judge_error"]

    async def test_a_judge_needs_an_answer(self, client, tokens, rag, superuser, models) -> None:
        base, *_ = await corpus(rag, superuser)
        response = await _ask(client, auth_header(tokens, superuser), base, judge_model="judge")
        assert response.status_code == 422

    async def test_without_an_answer_model_nothing_is_called(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        body = (await _ask(client, auth_header(tokens, superuser), base)).json()
        assert body["answer"] is None and body["answer_error"] is None
        assert models.provider.calls == [] and body["history_id"]

    async def test_no_history_when_retention_is_zero(
        self, app, client, tokens, rag, superuser, models, session, settings
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        app.dependency_overrides[get_playground_service] = lambda: PlaygroundService(
            rag.service,
            KnowledgeBaseRepository(session),
            RetrievalQueryRepository(session),
            None,
            history_days=0,
        )
        try:
            body = (await _ask(client, auth_header(tokens, superuser), base)).json()
        finally:
            app.dependency_overrides.pop(get_playground_service, None)
        assert body["history_id"] is None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class TestHistory:
    async def test_each_person_sees_their_own(
        self, client, tokens, rag, superuser, models, session, settings
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        reader = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="e62kv")
        mine = (await _ask(client, auth_header(tokens, superuser), base)).json()["history_id"]
        theirs = (await _ask(client, auth_header(tokens, reader), base)).json()["history_id"]

        listed = await client.get("/api/v1/retrieval/history", headers=auth_header(tokens, reader))
        assert [e["id"] for e in listed.json()] == [theirs]
        # Not another person's, not even by id.
        other = await client.get(
            f"/api/v1/retrieval/history/{mine}", headers=auth_header(tokens, reader)
        )
        assert other.status_code == 404
        everyone = await client.get(
            "/api/v1/retrieval/history",
            headers=auth_header(tokens, reader),
            params={"everyone": "true", "knowledge_base_id": str(base.id)},
        )
        assert everyone.status_code == 403
        denied = await client.delete(
            f"/api/v1/retrieval/history/{mine}", headers=auth_header(tokens, reader)
        )
        assert denied.status_code == 404

    async def test_a_manager_sees_what_was_asked_but_not_what_was_read(
        self, client, tokens, rag, superuser, models, session, settings
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        reader = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="e62asker")
        theirs = (
            await _ask(
                client, auth_header(tokens, reader), base, answer_model="chat", judge_model="j"
            )
        ).json()["history_id"]
        headers = auth_header(tokens, superuser)

        needs_base = await client.get(
            "/api/v1/retrieval/history", headers=headers, params={"everyone": "true"}
        )
        assert needs_base.status_code == 422
        listed = await client.get(
            "/api/v1/retrieval/history",
            headers=headers,
            params={"everyone": "true", "knowledge_base_id": str(base.id)},
        )
        entry = next(e for e in listed.json() if e["id"] == theirs)
        assert entry["mine"] is False and entry["username"] == reader.username

        detail = (await client.get(f"/api/v1/retrieval/history/{theirs}", headers=headers)).json()
        assert detail["query"] == "reset password" and detail["faithfulness"] == 0.5
        assert detail["answer"] is None
        assert all(r["text"] is None for r in detail["results"])
        assert "claims" not in detail["verdict"]

    async def test_filter_by_base_delete_and_clear(
        self, client, tokens, rag, superuser, models
    ) -> None:
        first, *_ = await corpus(rag, superuser)
        second, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        a = (await _ask(client, headers, first)).json()["history_id"]
        b = (await _ask(client, headers, second)).json()["history_id"]

        only = await client.get(
            "/api/v1/retrieval/history",
            headers=headers,
            params={"knowledge_base_id": str(first.id)},
        )
        ids = [e["id"] for e in only.json()]
        assert a in ids and b not in ids

        assert (
            await client.delete(f"/api/v1/retrieval/history/{a}", headers=headers)
        ).status_code == 200
        assert (
            await client.get(f"/api/v1/retrieval/history/{a}", headers=headers)
        ).status_code == 404
        cleared = await client.delete("/api/v1/retrieval/history", headers=headers)
        assert cleared.status_code == 200
        remaining = await client.get("/api/v1/retrieval/history", headers=headers)
        assert b not in [e["id"] for e in remaining.json()]

    async def test_a_replay_shows_what_changed(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        before = (
            await _ask(client, headers, base, top_k=3, answer_model="chat", judge_model="j")
        ).json()
        # The documents change: a new, better passage arrives; the judge is happier.
        await add_document(
            rag, base, "reset.txt", "Reset password reset password portal steps.", superuser
        )
        models.provider.judge = json.dumps(
            {"claims": [{"claim": "Use the portal.", "supported": True, "passages": [1]}]}
        )

        replay = await client.post(
            f"/api/v1/retrieval/history/{before['history_id']}/replay", headers=headers
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["previous"]["id"] == before["history_id"]
        assert "reset.txt" in body["changes"]["added"]
        assert body["changes"]["faithfulness_before"] == 0.5
        assert body["changes"]["faithfulness_after"] == 1.0
        assert body["current"]["verdict"]["faithfulness"] == 1.0
        saved = await client.get(
            f"/api/v1/retrieval/history/{body['current']['history_id']}", headers=headers
        )
        assert saved.json()["replay_of"] == before["history_id"]

    async def test_retention_prunes_old_questions(
        self, client, tokens, rag, superuser, models, session
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        entry_id = (await _ask(client, auth_header(tokens, superuser), base)).json()["history_id"]
        await session.execute(
            update(RetrievalQuery)
            .where(RetrievalQuery.id == uuid.UUID(entry_id))
            .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=40))
        )
        pruned = await RetrievalQueryRepository(session).prune(
            older_than=dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        )
        assert pruned >= 1
        left = await session.execute(
            select(RetrievalQuery.id).where(RetrievalQuery.id == uuid.UUID(entry_id))
        )
        assert left.first() is None


# ---------------------------------------------------------------------------
# Evaluation runs
# ---------------------------------------------------------------------------
class TestEvaluationAnswers:
    async def test_a_run_scores_faithfulness_and_correctness(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, howto, _, travel = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        created = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets",
            headers=headers,
            json={
                "name": "answers",
                "cases": [
                    {
                        "question": "reset password",
                        "expected_document_ids": [str(howto.id)],
                        "expected_answer": "Use the self service portal.",
                    },
                    {"question": "travel expenses", "expected_document_ids": [str(travel.id)]},
                ],
            },
        )
        assert created.status_code == 201, created.text
        models.provider.judge = json.dumps(
            {
                "claims": [
                    {"claim": "a", "supported": True, "passages": [1]},
                    {"claim": "b", "supported": False, "passages": []},
                ],
                "correctness": "agrees",
                "correctness_reason": "Same facts.",
            }
        )

        run = await client.post(
            f"/api/v1/eval-sets/{created.json()['id']}/runs",
            headers=headers,
            json={"reranker": "none", "answer_model": "chat", "judge_model": "judge"},
        )
        assert run.status_code == 201, run.text
        body = run.json()
        summary = body["summary"]
        assert (summary["answered"], summary["judged"], summary["answer_errors"]) == (2, 2, 0)
        assert summary["faithfulness"] == 0.5
        # Correctness only where the question has an expected answer.
        assert summary["correctness"] == 1.0 and summary["with_reference"] == 1
        assert body["config"]["answer_model"] == "chat"
        first, second = body["results"]
        assert first["answer"] and first["verdict"]["correctness_label"] == "agrees"
        assert second["verdict"]["correctness"] is None
        # The reference answer went to the judge only for the first question.
        judged = [c for c in models.provider.calls if c["judge"]]
        assert "REFERENCE ANSWER" in judged[0]["messages"][1].content
        assert "REFERENCE ANSWER" not in judged[1]["messages"][1].content

    async def test_an_answer_model_failing_is_recorded_per_question(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        created = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets",
            headers=headers,
            json={"name": "fails", "cases": [{"question": "reset password"}]},
        )
        run = await client.post(
            f"/api/v1/eval-sets/{created.json()['id']}/runs",
            headers=headers,
            json={"answer_model": "not-served"},
        )
        assert run.status_code == 201
        summary = run.json()["summary"]
        assert summary["answer_errors"] == 1 and summary["faithfulness"] is None
        assert "not-served" in run.json()["results"][0]["answer_error"]

    async def test_without_answer_settings_the_summary_is_unchanged(
        self, client, tokens, rag, superuser, models
    ) -> None:
        base, *_ = await corpus(rag, superuser)
        headers = auth_header(tokens, superuser)
        created = await client.post(
            f"/api/v1/knowledge-bases/{base.id}/eval-sets",
            headers=headers,
            json={"name": "plain", "cases": [{"question": "reset password"}]},
        )
        run = await client.post(
            f"/api/v1/eval-sets/{created.json()['id']}/runs", headers=headers, json={}
        )
        assert "faithfulness" not in run.json()["summary"]
        assert models.provider.calls == []
