# Speech, OCR and vision (M26, M28)

Four modalities, and the platform implements none of them. It routes to engines that do,
exactly as it routes chat to vLLM — same alias resolution, same deployment lifecycle,
same usage accounting. What Phase 9 adds is the routing, the pipeline and the mocks that
make all of it testable without a GPU.

| | surface | engine | alias |
|---|---|---|---|
| Speech to text | `POST /v1/audio/transcriptions` | faster-whisper | `enterprise-transcribe` |
| Text to speech | `POST /v1/audio/speech` | Fish Speech | `enterprise-speak` |
| OCR | the ingestion pipeline (§M15) | PaddleOCR | `enterprise-ocr` |
| Vision | `POST /v1/chat/completions` | any VLM | your own |

## Arabic is not an afterthought

Both speech surfaces carry a language, and **the platform never assumes one**. Omitting
`language` means the engine detects it — that is the default, and it matters more than it
looks: forcing `en` onto Arabic speech does not fail, it returns fluent, confident
nonsense that no downstream check will catch.

The choice of engine follows from the same concern. `large-v3` rather than a distilled
Whisper because the `distil-*` checkpoints are English-only, and `large-v3-turbo` — much
faster — is measurably weaker on Arabic dialects. A platform whose users dictate in both
languages should not pay for speed in the language it is least able to verify.

OCR recognises both scripts by default (`KNOWLEDGE__OCR_LANGUAGES=en,ar`). A document
with an Arabic body and English identifiers is the normal case here, not an edge one.

## Transcription and synthesis

Both follow OpenAI's audio API, so the stock client works unmodified:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="aip_...")

with open("briefing.wav", "rb") as audio:
    transcript = client.audio.transcriptions.create(
        model="enterprise-transcribe",     # an alias, not a container
        file=audio,
    )

speech = client.audio.speech.create(
    model="enterprise-speak", voice="ar-1", input="صباح الخير"
)
speech.stream_to_file("greeting.wav")
```

Two details worth knowing:

**`response_format=json` returns exactly `{"text": ...}`**, as the protocol specifies.
Ask for `verbose_json` to get the language, the duration and segment timestamps. A stock
client's parser is entitled not to see fields it did not request.

**Synthesis returns audio bytes, and the `Content-Type` states what was actually
produced** — not what was asked for. An engine that ignores an unsupported format and
returns WAV would otherwise have its output labelled `audio/mp3`, which yields a file
that downloads and then will not play.

Keys can be scoped to `audio` like any other surface, and audio is metered: seconds for
transcription, characters for synthesis, since neither has tokens. Both appear in
`/api/v1/usage` beside chat.

## OCR

Not an endpoint — a stage of ingestion. See [rag.md](rag.md): a scanned page runs
`PARSING → OCR → CHUNKING → EMBEDDING → INDEXED` and its chunks cite their page numbers,
so an answer built on a scan can be checked against the original.

With no OCR model deployed, an image lands in `NO_TEXT` with a reason naming what to
deploy. That is the honest state: the platform is not broken and the file is not corrupt,
there is simply no text yet.

OCR has no OpenAI-style standard, so the contract is the platform's own and deliberately
tiny — `POST /ocr` with a multipart image, returning
`{"blocks": [{"text", "page", "confidence"}], "language"}`.

## Vision

**There is no vision engine, and that is not an omission.** A vision model answers chat
completions whose messages carry image parts — it is an LLM with a different input
modality, and the gateway already forwards message content verbatim. Registering one as
type `VISION` or `MULTIMODAL` and deploying it is the whole of the work; callers then use
`/v1/chat/completions` exactly as they do for text.

Inventing a `VisionProvider` would have forked the chat path in two for a difference the
protocol does not make.

## Deploying the real engines

```bash
cp models/manifests/examples/faster-whisper-large-v3.yaml models/manifests/
# add:  endpoint_url: http://whisper.internal:8000
curl -X POST .../api/v1/models/import-manifests -H "Authorization: Bearer $TOKEN"
```

The examples ship under `models/manifests/examples/`, which is **not** scanned. Each
declares `runtime: external` — the platform points at an engine somebody else started —
and the platform rightly refuses such a manifest without an `endpoint_url`. That URL is
site-specific, so there is no value this repository could ship that would be right
anywhere.

PaddleOCR is declared with `min_gpu_count: 0`: it runs acceptably on CPU, OCR happens in
a queue rather than in a request, and a site with one GPU should spend it on inference.

## GPU-free development

`mock-asr`, `mock-tts` and `mock-ocr` import automatically and are all served by the one
`mock-vllm` container — three routes on one server rather than three engines. The
platform cannot tell the difference, which is the point.

They are openly synthetic and say so in their own output. What they model faithfully is
what the platform depends on: the language travels, the uploaded bytes are the bytes
transcribed, and synthesis returns a real RIFF/WAVE file rather than arbitrary bytes that
would let a truncated or mislabelled response pass unnoticed.

```bash
make gate-phase9
```

The gate deploys all four mocks, transcribes English and Arabic through an alias,
synthesises a playable file, pushes a real PNG through the ingestion pipeline until it is
**retrievable by search**, and checks that audio reached the usage records.
