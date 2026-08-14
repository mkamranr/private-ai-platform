"""HTTP-backed speech and OCR engines (Rule 8, §28, Phase 9).

The same design as `VLLMProvider`: one implementation per surface, serving both the real
engine and `mock-vllm`, because both speak the same wire protocol. The substitution for
GPU-free development happens at the container image, not in the platform — a second
"mock" code path is the one that never runs in production and therefore silently rots.

**Transcription and synthesis follow OpenAI's audio API** (`/v1/audio/transcriptions`,
`/v1/audio/speech`), which faster-whisper servers and most TTS servers already expose.
That is not deference to a vendor: it means the platform's own gateway can pass a
request through nearly unchanged, and a caller can point the stock `openai` client at
this platform for audio exactly as Phase 2 let them for chat.

**OCR has no such standard**, so `/ocr` is the platform's own shape — a multipart upload
in, blocks with page and confidence out. It is documented in docs/rag.md and implemented
by mock-vllm; a PaddleOCR deployment needs a thin server exposing the same two fields.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.interfaces.media import (
    OcrBlock,
    OcrEngine,
    OcrResult,
    SpeechToText,
    SynthesizedSpeech,
    TextToSpeech,
    Transcript,
    TranscriptSegment,
    VoiceDescriptor,
)
from app.core.logging import get_logger
from app.services.llm_provider import ProviderError

log = get_logger(__name__)

#: Generous, like the LLM timeout and for the same reason: transcribing an hour of audio
#: on a busy engine legitimately takes minutes. The gateway applies its own client-facing
#: deadline; this only guards against a wedged runtime.
DEFAULT_TIMEOUT = 600.0
HEALTH_TIMEOUT = 5.0


class _HttpEngine:
    """Shared plumbing: one base URL, one client per call, errors mapped consistently."""

    def __init__(
        self, base_url: str, *, timeout: float = DEFAULT_TIMEOUT, health_path: str = "/health"
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._health_path = health_path
        self._timeout = timeout

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}{path}", json=json, data=data, files=files
                )
        except httpx.HTTPError as exc:
            # The engine's own address never reaches the caller (§12): an internal
            # container URL in an error response is both useless to a developer and a
            # map of the internal network.
            raise ProviderError(f"The engine could not be reached: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            raise ProviderError(_engine_error(response))
        return response

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                response = await client.get(f"{self._base_url}{self._health_path}")
            return response.status_code < 400
        except httpx.HTTPError:
            return False


class HttpSpeechToText(_HttpEngine, SpeechToText):
    """Transcription against an OpenAI-compatible audio endpoint (M26)."""

    async def transcribe(
        self,
        audio: bytes,
        *,
        model: str,
        filename: str = "audio.wav",
        language: str | None = None,
        prompt: str | None = None,
        timestamps: bool = False,
    ) -> Transcript:
        form: dict[str, Any] = {"model": model, "response_format": "json"}
        # Omitted rather than sent empty when unknown: engines treat an empty `language`
        # as a request for a language literally named "", and faster-whisper's detection
        # is better than any default this platform could pick.
        if language:
            form["language"] = language
        if prompt:
            form["prompt"] = prompt
        if timestamps:
            form["timestamp_granularities"] = "segment"

        response = await self._post(
            "/v1/audio/transcriptions",
            data=form,
            files={"file": (filename, audio, "application/octet-stream")},
        )
        body = response.json()
        return Transcript(
            text=body.get("text", ""),
            language=body.get("language"),
            duration_seconds=float(body.get("duration") or 0.0),
            segments=tuple(
                TranscriptSegment(
                    text=segment.get("text", ""),
                    start_seconds=float(segment.get("start") or 0.0),
                    end_seconds=float(segment.get("end") or 0.0),
                )
                for segment in body.get("segments") or ()
            ),
        )


class HttpTextToSpeech(_HttpEngine, TextToSpeech):
    """Synthesis against an OpenAI-compatible audio endpoint (M26)."""

    async def synthesize(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        audio_format: str = "wav",
        speed: float = 1.0,
    ) -> SynthesizedSpeech:
        response = await self._post(
            "/v1/audio/speech",
            json={
                "model": model,
                "input": text,
                "voice": voice,
                "response_format": audio_format,
                "speed": speed,
            },
        )
        # Taken from the response, not from the request: an engine may ignore an
        # unsupported format, and labelling WAV bytes as mp3 produces a file that
        # downloads and then will not play.
        content_type = response.headers.get("content-type", "")
        produced = content_type.removeprefix("audio/").split(";")[0].strip() or audio_format
        return SynthesizedSpeech(audio=response.content, audio_format=produced)

    async def voices(self) -> list[VoiceDescriptor]:
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                response = await client.get(f"{self._base_url}/v1/audio/voices")
            if response.status_code >= 400:
                return []
        except httpx.HTTPError:
            # An empty list, not an error: not every engine publishes its voices, and a
            # caller asking "what can you say this in" should not get a 502 because the
            # engine has no such route.
            return []
        return [
            VoiceDescriptor(
                id=voice.get("id", ""),
                language=voice.get("language", ""),
                display_name=voice.get("display_name", ""),
            )
            for voice in response.json().get("voices", [])
        ]


class HttpOcrEngine(_HttpEngine, OcrEngine):
    """Text recognition against the platform's `/ocr` shape (M28)."""

    async def recognise(
        self,
        image: bytes,
        *,
        model: str,
        filename: str = "page.png",
        languages: tuple[str, ...] = ("en", "ar"),
    ) -> OcrResult:
        response = await self._post(
            "/ocr",
            data={"model": model, "languages": ",".join(languages)},
            files={"file": (filename, image, "application/octet-stream")},
        )
        body = response.json()
        return OcrResult(
            blocks=tuple(
                OcrBlock(
                    text=block.get("text", ""),
                    page=int(block.get("page") or 1),
                    confidence=(
                        float(block["confidence"]) if block.get("confidence") is not None else None
                    ),
                )
                for block in body.get("blocks") or ()
            ),
            language=body.get("language"),
            warning=body.get("warning"),
        )


def _engine_error(response: httpx.Response) -> str:
    """The engine's own message when it gives one, never its address."""
    try:
        payload = response.json()
    except ValueError:
        return f"The engine returned HTTP {response.status_code}."
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return f"The engine returned HTTP {response.status_code}."


class ResolvingOcrEngine(OcrEngine):
    """An `OcrEngine` that resolves its alias on every call.

    Bound to a resolver rather than to a URL, because a deployment can be replaced while
    a queue of documents is draining. Pinning the address at construction would send the
    rest of the batch to a container that no longer exists, and the failure would look
    like OCR being broken rather than a deployment having moved.
    """

    def __init__(self, resolve: Any) -> None:
        # `resolve(alias) -> (engine, served_model_name)`; see
        # GatewayService.ocr_engine_for_model.
        self._resolve = resolve

    async def recognise(
        self,
        image: bytes,
        *,
        model: str,
        filename: str = "page.png",
        languages: tuple[str, ...] = ("en", "ar"),
    ) -> OcrResult:
        engine, served_model_name = await self._resolve(model)
        result: OcrResult = await engine.recognise(
            image, model=served_model_name, filename=filename, languages=languages
        )
        return result

    async def health(self) -> bool:
        return True
