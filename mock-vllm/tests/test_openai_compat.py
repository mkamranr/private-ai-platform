"""Mock vLLM OpenAI-protocol conformance.

The value of this service is that the platform cannot tell it apart from real vLLM.
These tests pin the parts of the protocol the platform actually depends on — if the mock
drifts, the gateway's streaming and accounting would pass locally and fail on hardware.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from app import main


@pytest.fixture(autouse=True)
def _ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the simulated load delay for everything except the readiness tests."""
    monkeypatch.setattr(main, "STARTUP_DELAY", 0.0)
    monkeypatch.setattr(main, "TOKEN_DELAY", 0.0)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://mock") as http:
        yield http


def _sse_events(body: str) -> list[dict]:
    """Parse an SSE stream into its JSON payloads, dropping the [DONE] sentinel."""
    events = []
    for block in body.split("\n\n"):
        line = block.strip()
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ").strip()
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


class TestReadiness:
    async def test_health_is_503_while_loading(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deployment worker's HEALTH_CHECK phase needs a real transition to wait
        on. A mock that was instantly ready would let a broken wait loop pass here and
        only fail against a real 30B model, minutes into a production deploy."""
        monkeypatch.setattr(main, "STARTUP_DELAY", 3600.0)
        assert (await client.get("/health")).status_code == 503

    async def test_health_ok_once_loaded(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_inference_refused_while_loading(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main, "STARTUP_DELAY", 3600.0)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 503


class TestModels:
    async def test_lists_the_served_model(self, client: AsyncClient) -> None:
        body = (await client.get("/v1/models")).json()
        assert body["object"] == "list"
        assert body["data"][0]["id"] == main.SERVED_MODEL
        assert body["data"][0]["object"] == "model"


class TestChatCompletions:
    async def test_non_streaming_shape(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen3-30b", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["model"] == "qwen3-30b"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert body["choices"][0]["finish_reason"] == "stop"
        assert body["usage"]["total_tokens"] > 0
        assert (
            body["usage"]["total_tokens"]
            == body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
        )

    async def test_response_announces_itself_as_synthetic(self, client: AsyncClient) -> None:
        """A mock returning plausible prose would make a misconfigured production
        deployment look like a working one."""
        body = (
            await client.post(
                "/v1/chat/completions",
                json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            )
        ).json()
        assert "[mock-vllm]" in body["choices"][0]["message"]["content"]

    async def test_max_tokens_truncates(self, client: AsyncClient) -> None:
        body = (
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 5,
                },
            )
        ).json()
        assert len(body["choices"][0]["message"]["content"].split()) <= 5

    async def test_streaming_is_sse_framed(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.rstrip().endswith("data: [DONE]")

    async def test_stream_emits_role_then_content_then_finish(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        events = _sse_events(response.text)
        assert events[0]["choices"][0]["delta"]["role"] == "assistant"
        assert any(e["choices"] and e["choices"][0]["delta"].get("content") for e in events)
        assert any(e["choices"] and e["choices"][0]["finish_reason"] == "stop" for e in events)

    async def test_stream_yields_many_chunks(self, client: AsyncClient) -> None:
        """One chunk containing everything would be indistinguishable from a
        non-streaming response and would hide a buffering regression."""
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        content_chunks = [
            e
            for e in _sse_events(response.text)
            if e["choices"] and e["choices"][0]["delta"].get("content")
        ]
        assert len(content_chunks) > 5

    async def test_usage_omitted_unless_requested(self, client: AsyncClient) -> None:
        """Matches vLLM: no usage in a stream unless stream_options asks for it."""
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        assert not any(e.get("usage") for e in _sse_events(response.text))

    async def test_include_usage_emits_a_final_usage_chunk(self, client: AsyncClient) -> None:
        """**The chunk the gateway's accounting depends on.** A proxy that never buffers
        can only learn token counts from this; without it, streamed traffic silently
        records zero and every usage report is wrong."""
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        events = _sse_events(response.text)
        usage_events = [e for e in events if e.get("usage")]
        assert len(usage_events) == 1

        final = usage_events[0]
        assert final["choices"] == [], "the usage chunk must carry no delta"
        assert final["usage"]["prompt_tokens"] > 0
        assert final["usage"]["completion_tokens"] > 0
        assert events[-1] is final, "usage must be the last event before [DONE]"

    async def test_streamed_and_buffered_usage_agree(self, client: AsyncClient) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "count tokens"}]}
        buffered = (await client.post("/v1/chat/completions", json=payload)).json()
        streamed = _sse_events(
            (
                await client.post(
                    "/v1/chat/completions",
                    json={**payload, "stream": True, "stream_options": {"include_usage": True}},
                )
            ).text
        )
        streamed_usage = next(e["usage"] for e in streamed if e.get("usage"))
        assert streamed_usage["prompt_tokens"] == buffered["usage"]["prompt_tokens"]


class TestCompletions:
    async def test_legacy_completions_shape(self, client: AsyncClient) -> None:
        body = (await client.post("/v1/completions", json={"model": "m", "prompt": "hello"})).json()
        assert body["object"] == "text_completion"
        assert body["choices"][0]["text"]
        assert body["usage"]["total_tokens"] > 0


class TestEmbeddings:
    async def test_single_input(self, client: AsyncClient) -> None:
        body = (await client.post("/v1/embeddings", json={"model": "e", "input": "hello"})).json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert len(body["data"][0]["embedding"]) == main.EMBEDDING_DIM

    async def test_batch_input_preserves_order(self, client: AsyncClient) -> None:
        body = (
            await client.post("/v1/embeddings", json={"model": "e", "input": ["a", "b", "c"]})
        ).json()
        assert [d["index"] for d in body["data"]] == [0, 1, 2]

    async def test_deterministic_for_identical_text(self, client: AsyncClient) -> None:
        """Identical inputs collapsing to identical vectors is what makes a
        de-duplication or caching bug in Phase 5 visible."""
        first = (await client.post("/v1/embeddings", json={"model": "e", "input": "same"})).json()[
            "data"
        ][0]["embedding"]
        second = (await client.post("/v1/embeddings", json={"model": "e", "input": "same"})).json()[
            "data"
        ][0]["embedding"]
        assert first == second

    async def test_different_text_gives_different_vectors(self, client: AsyncClient) -> None:
        body = (
            await client.post("/v1/embeddings", json={"model": "e", "input": ["one", "two"]})
        ).json()
        assert body["data"][0]["embedding"] != body["data"][1]["embedding"]

    async def test_vectors_are_unit_normalised(self, client: AsyncClient) -> None:
        """Cosine similarity downstream assumes unit vectors, as real embedding
        models produce."""
        vector = (
            await client.post("/v1/embeddings", json={"model": "e", "input": "hello"})
        ).json()["data"][0]["embedding"]
        norm = sum(v * v for v in vector) ** 0.5
        assert abs(norm - 1.0) < 1e-6


class TestMetrics:
    async def test_prometheus_exposition(self, client: AsyncClient) -> None:
        """Real vLLM exports these names, so Phase 7's scrape config and dashboards can
        be built before any GPU is involved."""
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "vllm:num_requests_running" in response.text
        assert "vllm:model_ready 1" in response.text


class TestAudio:
    """Speech surfaces (§M26, Phase 9).

    What matters is not the words — they are openly synthetic — but that the right bytes
    arrive, that the language is carried rather than assumed, and that what comes back is
    a file something can actually play.
    """

    async def test_transcription_reflects_the_uploaded_file(self, client: AsyncClient) -> None:
        """Names the file and its size, so a caller can prove the *right* audio arrived.

        The failure this catches is a pipeline that transcribes a different upload than
        the one requested, which a fixed response would hide completely.
        """
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("meeting.wav", b"\x00" * 3200, "audio/wav")},
            data={"model": "mock-asr"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "meeting.wav" in body["text"]
        assert "3200 bytes" in body["text"]
        assert body["duration"] > 0

    async def test_language_is_detected_not_assumed(self, client: AsyncClient) -> None:
        """Arabic is half of what this platform is for. Forcing `en` onto Arabic speech
        does not fail, it returns confident nonsense — so the language must travel."""
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("briefing_ar_01.wav", b"\x00" * 1600, "audio/wav")},
            data={"model": "mock-asr"},
        )
        body = response.json()
        assert body["language"] == "ar"
        # Real Arabic, so anything mishandling non-ASCII or RTL breaks here rather than
        # on a customer's recording.
        assert any("؀" <= ch <= "ۿ" for ch in body["text"])

    async def test_an_explicit_language_wins(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("briefing_ar_01.wav", b"\x00" * 1600, "audio/wav")},
            data={"model": "mock-asr", "language": "en"},
        )
        assert response.json()["language"] == "en"

    async def test_timestamps_are_returned_when_asked_for(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"\x00" * 3200, "audio/wav")},
            data={"model": "mock-asr", "timestamp_granularities": "segment"},
        )
        segments = response.json()["segments"]
        assert segments and segments[0]["end"] > segments[0]["start"]

    async def test_speech_returns_a_playable_wav(self, client: AsyncClient) -> None:
        """A real RIFF header, not arbitrary bytes.

        Arbitrary bytes would let a truncated response or a wrong content type pass
        unnoticed — the caller gets a file that downloads and then will not play.
        """
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "mock-tts", "input": "hello there", "voice": "mock-en-1"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content[:4] == b"RIFF"
        assert response.content[8:12] == b"WAVE"

    async def test_an_unsupported_format_is_refused_not_mislabelled(
        self, client: AsyncClient
    ) -> None:
        """Returning WAV bytes under an mp3 content type is worse than an error."""
        response = await client.post(
            "/v1/audio/speech",
            json={"model": "mock-tts", "input": "hi", "response_format": "mp3"},
        )
        assert response.status_code == 400
        assert "unsupported_format" in response.text

    async def test_voices_cover_both_languages(self, client: AsyncClient) -> None:
        voices = (await client.get("/v1/audio/voices")).json()["voices"]
        assert {v["language"] for v in voices} == {"en", "ar"}


class TestOcr:
    async def test_ocr_reflects_the_uploaded_image(self, client: AsyncClient) -> None:
        response = await client.post(
            "/ocr",
            files={"file": ("scan.png", b"\x89PNG" + b"\x00" * 100, "image/png")},
            data={"model": "mock-ocr"},
        )
        assert response.status_code == 200
        block = response.json()["blocks"][0]
        assert "scan.png" in block["text"]
        assert block["page"] == 1
