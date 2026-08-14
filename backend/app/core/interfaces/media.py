"""Speech, OCR and vision engines (Rule 8, §28, Phase 9).

Three seams, kept apart because the engines behind them are unrelated and replaced
independently: an operator who swaps faster-whisper for a hosted ASR has no reason to
touch OCR. ``MockMediaProvider`` implements all three so the pipeline is exercisable on
a machine with no GPU, exactly as ``MockLLMProvider`` does for chat.

**Vision has no interface here, and that is deliberate.** A vision model answers chat
completions whose messages carry image parts — it *is* an LLM with a different input
modality, and the gateway already forwards message content verbatim. Inventing a
`VisionProvider` would fork the chat path in two for a difference the protocol does not
make; what the platform needs instead is to know a model *can* accept images, which the
registry records as `supports_vision` and the model type `VISION`/`MULTIMODAL`.

Audio is passed as bytes, never as a path. The control plane and the runtime are
different containers on different hosts (§12), so a filename is meaningless across that
boundary — and a shared volume for uploads is the coupling §28 exists to prevent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One timed span of recognised speech.

    Timestamps make a transcript checkable: a citation into an hour of audio that cannot
    be played back at the right moment is not much better than no citation. Same
    reasoning as the page numbers on document chunks (§M15).
    """

    text: str
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    #: BCP-47-ish, as reported by the engine — "ar", "en". Detected rather than assumed:
    #: this platform's users write and speak both, often in the same recording.
    language: str | None = None
    duration_seconds: float = 0.0
    segments: tuple[TranscriptSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class SynthesizedSpeech:
    audio: bytes
    #: The container actually produced, which is not always the one requested — an engine
    #: may ignore an unsupported format, and a caller writing `.mp3` onto WAV bytes gets a
    #: file nothing will play.
    audio_format: str
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class OcrBlock:
    text: str
    #: 1-based page for a multi-page source, so an OCR'd chunk cites a page like a parsed
    #: one does. Images are page 1.
    page: int = 1
    #: The engine's own 0-1 score. Kept per block rather than averaged over the document:
    #: one unreadable stamp in an otherwise clean scan should not drag the whole file's
    #: confidence down and hide that everything else was fine.
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class OcrResult:
    blocks: tuple[OcrBlock, ...] = ()
    language: str | None = None
    warning: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if block.text)


@dataclass(frozen=True, slots=True)
class VoiceDescriptor:
    id: str
    language: str
    display_name: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


class SpeechToText(ABC):
    """Transcription (M26). One instance is bound to one served endpoint."""

    @abstractmethod
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
        """Transcribe. ``language=None`` asks the engine to detect it.

        Detection matters here rather than being a convenience: forcing `en` on Arabic
        speech does not fail, it returns confident nonsense.
        """
        ...

    @abstractmethod
    async def health(self) -> bool: ...


class TextToSpeech(ABC):
    """Synthesis (M26)."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        model: str,
        voice: str,
        audio_format: str = "wav",
        speed: float = 1.0,
    ) -> SynthesizedSpeech: ...

    @abstractmethod
    async def voices(self) -> list[VoiceDescriptor]:
        """What this engine can speak with. Listed rather than configured: the set
        depends on the loaded checkpoint, and a hard-coded list goes stale silently."""
        ...

    @abstractmethod
    async def health(self) -> bool: ...


class OcrEngine(ABC):
    """Text recognition in images and scanned pages (M28)."""

    @abstractmethod
    async def recognise(
        self,
        image: bytes,
        *,
        model: str,
        filename: str = "page.png",
        languages: tuple[str, ...] = ("en", "ar"),
    ) -> OcrResult:
        """Recognise text. Both scripts by default, because a document that mixes an
        Arabic body with English identifiers is the normal case here, not an edge one."""
        ...

    @abstractmethod
    async def health(self) -> bool: ...
