"""Speech surfaces and the OCR ingestion path (M26, M28 — Phase 9).

Weighted towards the two things that are easy to get wrong and hard to notice.

**The language must travel.** Arabic is half of what this platform is for, and forcing
`en` onto Arabic speech does not fail — it returns confident nonsense. So the tests
assert the language reaches the engine and comes back, rather than that a transcript
was produced.

**Audio is bytes, not JSON.** A surface that returns a base64 string, or labels WAV
bytes as mp3, produces a file that downloads and will not play. That is invisible to a
test asserting only on status codes.
"""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient

from app.core.interfaces.media import OcrBlock, OcrResult, SynthesizedSpeech, Transcript
from app.services.document_parsers import parse


class FakeSpeechEngine:
    """Stands in for a served ASR/TTS runtime, recording what it was asked for."""

    def __init__(self) -> None:
        self.last_language: str | None = None
        self.last_filename: str | None = None
        self.last_audio: bytes = b""

    async def transcribe(self, audio, *, model, filename="audio.wav", language=None, **kwargs):
        self.last_audio = audio
        self.last_filename = filename
        self.last_language = language
        return Transcript(
            text="مرحبا" if language == "ar" else "hello there",
            language=language or "en",
            duration_seconds=2.5,
        )

    async def synthesize(self, text, *, model, voice, audio_format="wav", speed=1.0):
        return SynthesizedSpeech(audio=b"RIFF....WAVE-audio", audio_format="wav")

    async def health(self) -> bool:
        return True


@pytest.fixture
async def audio_app(app, monkeypatch):
    """App whose gateway reaches a fake engine instead of a container."""
    from app.services import gateway as gateway_module

    engine = FakeSpeechEngine()
    monkeypatch.setattr(gateway_module, "HttpSpeechToText", lambda url: engine)
    monkeypatch.setattr(gateway_module, "HttpTextToSpeech", lambda url: engine)
    yield app, engine


class TestTranscription:
    async def test_audio_reaches_the_engine_intact(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """The bytes uploaded are the bytes transcribed.

        The failure this catches is a surface that transcribes a different upload — or an
        empty one — which a fixed fake response would hide entirely.
        """
        _, engine = audio_app
        payload = b"\x00\x01\x02" * 100
        response = await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("meeting.wav", io.BytesIO(payload), "audio/wav")},
            data={"model": committed_gateway["model_name"]},
        )
        assert response.status_code == 200, response.text
        assert engine.last_audio == payload
        assert engine.last_filename == "meeting.wav"
        assert response.json()["text"] == "hello there"

    async def test_the_requested_language_is_carried_through(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """Arabic is half of what this platform is for."""
        _, engine = audio_app
        response = await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("a.wav", io.BytesIO(b"\x00" * 32), "audio/wav")},
            data={"model": committed_gateway["model_name"], "language": "ar"},
        )
        assert engine.last_language == "ar"
        assert response.json()["text"] == "مرحبا"

    async def test_no_language_means_detect_not_english(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """`None` must reach the engine so it detects. Defaulting to `en` here would
        silently mistranscribe every Arabic recording."""
        _, engine = audio_app
        await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("a.wav", io.BytesIO(b"\x00" * 32), "audio/wav")},
            data={"model": committed_gateway["model_name"]},
        )
        assert engine.last_language is None

    async def test_plain_json_is_the_default_shape(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """OpenAI's `json` format is exactly {"text": ...}; a stock client's parser is
        entitled to see nothing else."""
        response = await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("a.wav", io.BytesIO(b"\x00" * 32), "audio/wav")},
            data={"model": committed_gateway["model_name"]},
        )
        assert set(response.json()) == {"text"}

    async def test_verbose_json_adds_language_and_duration(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("a.wav", io.BytesIO(b"\x00" * 32), "audio/wav")},
            data={"model": committed_gateway["model_name"], "response_format": "verbose_json"},
        )
        body = response.json()
        assert body["duration"] == 2.5
        assert body["language"] == "en"

    async def test_an_empty_upload_is_refused(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """Better than sending nothing to the engine and reporting its confusion."""
        response = await client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            files={"file": ("a.wav", io.BytesIO(b""), "audio/wav")},
            data={"model": committed_gateway["model_name"]},
        )
        assert response.status_code == 422

    async def test_transcription_needs_a_key(self, audio_app, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", io.BytesIO(b"\x00" * 32), "audio/wav")},
            data={"model": "anything"},
        )
        assert response.status_code == 401


class TestSpeech:
    async def test_speech_returns_audio_bytes_not_json(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """The protocol says bytes. A base64 string in a JSON envelope is a different
        API that no OpenAI client can read."""
        response = await client.post(
            "/v1/audio/speech",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={"model": committed_gateway["model_name"], "input": "hello", "voice": "en-1"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("audio/")
        assert response.content.startswith(b"RIFF")

    async def test_the_format_reported_is_the_one_produced(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        """The engine here produces wav whatever is asked for. Labelling that mp3 would
        yield a file that downloads and then will not play."""
        response = await client.post(
            "/v1/audio/speech",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={
                "model": committed_gateway["model_name"],
                "input": "hi",
                "response_format": "mp3",
            },
        )
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["content-disposition"].endswith('speech.wav"')

    async def test_empty_input_is_refused(
        self, audio_app, committed_gateway: dict, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/v1/audio/speech",
            headers={"Authorization": f"Bearer {committed_gateway['secret']}"},
            json={"model": committed_gateway["model_name"], "input": ""},
        )
        assert response.status_code == 422


class TestOcrPipeline:
    """OCR as a stage of ingestion (§M15, M28), not as an endpoint."""

    def test_an_image_is_routed_to_ocr_rather_than_parsed_to_nothing(self) -> None:
        """The seam Phase 9 fills. A PNG has no extractable text; saying so is what makes
        the difference between "needs OCR" and "this file is broken"."""
        result = parse("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")
        assert result.needs_ocr is True
        assert result.text_length == 0

    async def test_ocr_output_is_shaped_like_a_parse_result(self) -> None:
        """Deliberately the same type, so chunking, citation and embedding treat a scan
        exactly as they treat a typed page — there is no second pipeline.

        Exercised directly against the mapping rather than through a full ingestion run:
        what is being asserted is the shape conversion, and driving a worker, a queue and
        a vector store to reach it would test everything except that.
        """
        from types import SimpleNamespace

        from app.services.knowledge import KnowledgeService

        class FakeOcr:
            async def recognise(self, image, *, model, filename="p.png", languages=("en", "ar")):
                return OcrResult(
                    blocks=(
                        OcrBlock(text="First page text", page=1, confidence=0.98),
                        OcrBlock(text="", page=2),  # dropped: an empty block is not a chunk
                        OcrBlock(text="Second page text", page=2, confidence=0.91),
                    ),
                    language="en",
                )

            async def health(self) -> bool:
                return True

        service = KnowledgeService.__new__(KnowledgeService)
        service._ocr = FakeOcr()
        service._settings = SimpleNamespace(
            knowledge=SimpleNamespace(ocr_model="enterprise-ocr", ocr_languages=["en", "ar"])
        )
        parsed = await service._recognise(SimpleNamespace(filename="scan.png"), b"image-bytes")

        assert [s.text for s in parsed.segments] == ["First page text", "Second page text"]
        # The page becomes the citation location, so a chunk from a scan cites "page 2"
        # the way a chunk from a PDF does.
        assert [s.location for s in parsed.segments] == ["page 1", "page 2"]
