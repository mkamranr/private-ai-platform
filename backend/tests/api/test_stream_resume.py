"""E66: a stream that breaks after sending text is finished by another copy.

The copy that takes over is given the text already sent as a trailing assistant message,
with the options its runtime needs to continue it rather than start a new turn, and the
caller sees one answer.
"""

# ruff: noqa: F811  (pytest fixtures imported from the E58 tests)
from __future__ import annotations

import json
import uuid
from typing import Any

from app.core.interfaces.llm import ChatCompletionChunk, TokenUsage
from app.models.models_registry import ModelAlias
from app.services.gateway import GatewayContext, StreamAccounting
from app.services.llm_provider import ProviderError
from tests.api.test_replica_balancing import (  # noqa: F401  (fixtures)
    MESSAGES,
    _model_with_copies,
    _reset_breakers,
    gateway,
)


class Copy:
    """One copy of the model. ``breaks`` sends these pieces, then the connection drops."""

    def __init__(self, calls: list[dict[str, Any]], pieces: list[str], *, breaks: bool) -> None:
        self.calls, self.pieces, self.breaks = calls, pieces, breaks

    async def chat_stream(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        for piece in self.pieces:
            yield ChatCompletionChunk(id="s", model="m", delta=piece)
        if self.breaks:
            raise ProviderError("connection reset by peer")
        yield ChatCompletionChunk(id="s", model="m", delta="", finish_reason="stop")
        yield ChatCompletionChunk(
            id="s",
            model="m",
            delta="",
            usage=TokenUsage(prompt_tokens=30, completion_tokens=4, total_tokens=34),
        )


class ToolThenBreak(Copy):
    async def chat_stream(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        yield ChatCompletionChunk(id="s", model="m", delta="Let me look")
        call = {"index": 0, "id": "c1", "function": {"name": "lookup", "arguments": '{"q'}}
        yield ChatCompletionChunk(id="s", model="m", delta="", tool_calls=(call,))
        raise ProviderError("connection reset by peer")


def _text(sse: str) -> str:
    """The content the caller received, joined."""
    out = []
    for line in sse.splitlines():
        if line.startswith("data: {"):
            event = json.loads(line.removeprefix("data: "))
            for choice in event.get("choices", []):
                out.append(choice["delta"].get("content", ""))
    return "".join(out)


async def _stream(
    gateway,
    session,
    *,
    runtime="mock",
    first=None,
    second=None,
    body=None,
    build_args=None,
    **model,
):
    build, providers = gateway
    model_row, urls = await _model_with_copies(session, 2, runtime=runtime, **model)
    calls: list[dict[str, Any]] = []
    providers[urls[0]] = (first or Copy)(calls, ["The answer", " is"], breaks=True)
    providers[urls[1]] = (second or Copy)(calls, [" forty", " two."], breaks=False)
    service = build(strategy="first", **(build_args or {}))
    request = {"model": model_row.name, "messages": MESSAGES, "stream": True, **(body or {})}
    context = GatewayContext()
    target = await service.prepare_stream(request, context)
    accounting = StreamAccounting()
    sse = "".join([c async for c in service.stream_chunks(target, request, accounting)])
    await service.finalise_stream(target, context, accounting)
    return sse, calls, accounting, target, urls


class TestResume:
    async def test_the_caller_gets_one_answer(self, gateway, session):
        sse, calls, accounting, target, urls = await _stream(gateway, session)
        assert _text(sse) == "The answer is forty two."
        assert "upstream_error" not in sse and sse.rstrip().endswith("[DONE]")
        # The second copy was asked to continue what had been sent.
        resumed = calls[1]["messages"]
        assert resumed[-1].role == "assistant" and resumed[-1].content == "The answer is"
        assert resumed[0].content == "hi"
        assert accounting.resumed == 1 and accounting.status_code == 200
        assert accounting.completion_tokens == 4  # the resumed call's report
        assert target.internal_url == urls[1]
        # One stream id throughout.
        ids = {json.loads(x[6:])["id"] for x in sse.splitlines() if x.startswith("data: {")}
        assert len(ids) == 1

    async def test_vllm_is_told_to_continue_the_message(self, gateway, session):
        _, calls, *_ = await _stream(gateway, session, runtime="vllm", body={"max_tokens": 50})
        assert calls[0]["extra"] is None and calls[0]["max_tokens"] == 50
        assert calls[1]["extra"] == {
            "continue_final_message": True,
            "add_generation_prompt": False,
        }
        assert calls[1]["max_tokens"] == 48  # two pieces already sent

    async def test_a_half_sent_tool_call_is_not_resumed(self, gateway, session):
        sse, calls, accounting, *_ = await _stream(gateway, session, first=ToolThenBreak)
        assert len(calls) == 1 and "upstream_error" in sse
        assert accounting.status_code == 502 and accounting.resumed == 0

    async def test_a_model_that_cannot_resume_is_not(self, gateway, session):
        sse, calls, *_ = await _stream(gateway, session, capabilities=["chat", "streaming"])
        assert len(calls) == 1 and "upstream_error" in sse
        # External servers don't resume unless declared to.
        sse, calls, *_ = await _stream(gateway, session, runtime="external")
        assert len(calls) == 1 and "upstream_error" in sse

    async def test_too_long_an_answer_or_resuming_off(self, gateway, session):
        sse, calls, *_ = await _stream(gateway, session, build_args={"resume_chars": 5})
        assert len(calls) == 1 and "upstream_error" in sse
        sse, calls, *_ = await _stream(gateway, session, build_args={"resume_chars": 0})
        assert len(calls) == 1 and "upstream_error" in sse

    async def test_no_copy_left_reports_the_failure(self, gateway, session):
        sse, calls, *_ = await _stream(gateway, session, build_args={"retries": 0})
        assert len(calls) == 1 and "upstream_error" in sse
        assert _text(sse) == "The answer is"

    async def test_a_resumed_copy_that_breaks_too(self, gateway, session):
        class Breaks(Copy):
            def __init__(self, calls, pieces, *, breaks):
                super().__init__(calls, pieces, breaks=True)

        sse, calls, accounting, *_ = await _stream(
            gateway, session, second=Breaks, build_args={"retries": 1}
        )
        assert len(calls) == 2 and "upstream_error" in sse
        assert _text(sse) == "The answer is forty two."
        assert accounting.status_code == 502


class TestFallback:
    """E87: with no copy left, the alias's fallback carries on (or starts) the answer."""

    async def _aliased(self, gateway, session, *, capabilities=None, primary_copies=1):
        build, providers = gateway
        primary, primary_urls = await _model_with_copies(session, primary_copies)
        extra = {"capabilities": capabilities} if capabilities is not None else {}
        backup, backup_urls = await _model_with_copies(session, 1, **extra)
        tag = uuid.uuid4().hex[:8]
        spare = ModelAlias(alias=f"spare-{tag}", model_id=backup.id)
        session.add(spare)
        await session.flush()
        main = ModelAlias(alias=f"main-{tag}", model_id=primary.id, fallback_alias_id=spare.id)
        session.add(main)
        await session.flush()
        return build, providers, main, primary_urls, backup_urls, backup

    async def _run(self, service, alias):
        request = {"model": alias.alias, "messages": MESSAGES, "stream": True}
        context = GatewayContext()
        target = await service.prepare_stream(request, context)
        accounting = StreamAccounting()
        sse = "".join(
            [c async for c in service.stream_chunks(target, request, accounting, context)]
        )
        return sse, accounting, target

    async def test_resumed_on_the_fallback(self, gateway, session):
        build, providers, main, primary, backup, backup_model = await self._aliased(
            gateway, session
        )
        calls: list[dict[str, Any]] = []
        providers[primary[0]] = Copy(calls, ["The answer", " is"], breaks=True)
        providers[backup[0]] = Copy(calls, [" forty", " two."], breaks=False)
        sse, accounting, target = await self._run(build(strategy="first", retries=0), main)
        assert _text(sse) == "The answer is forty two." and "upstream_error" not in sse
        assert calls[1]["messages"][-1].content == "The answer is"
        assert accounting.resumed == 1
        # Recorded against the model that finished it, as a fallback of the alias.
        assert target.model.id == backup_model.id and target.fallback_from == main.alias

    async def test_a_fallback_that_cannot_resume_is_not_asked(self, gateway, session):
        build, providers, main, primary, backup, _ = await self._aliased(
            gateway, session, capabilities=["chat", "streaming"]
        )
        calls: list[dict[str, Any]] = []
        providers[primary[0]] = Copy(calls, ["The answer", " is"], breaks=True)
        providers[backup[0]] = Copy(calls, [" forty", " two."], breaks=False)
        sse, _, _ = await self._run(build(strategy="first", retries=0), main)
        assert len(calls) == 1 and "upstream_error" in sse

    async def test_before_any_text_the_fallback_answers(self, gateway, session):
        build, providers, main, primary, backup, backup_model = await self._aliased(
            gateway, session
        )
        calls: list[dict[str, Any]] = []
        providers[primary[0]] = Copy(calls, [], breaks=True)
        providers[backup[0]] = Copy(calls, ["Forty two."], breaks=False)
        sse, accounting, target = await self._run(build(strategy="first", retries=0), main)
        assert _text(sse) == "Forty two." and accounting.resumed == 0
        assert calls[1]["messages"][-1].role != "assistant"
        assert target.model.id == backup_model.id
