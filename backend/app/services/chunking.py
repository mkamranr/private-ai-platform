"""Text chunking (§M15).

    upload → parse → OCR if required → **chunk** → embed → Qdrant

Chunking is where retrieval quality is mostly won or lost, and the failure mode is quiet:
badly chunked text still embeds, still searches, still returns hits — they are just the
wrong hits, or fragments too mutilated for the model to use.

Three properties this implementation holds to:

**Split on structure before length.** Paragraph, then sentence, then — only if a single
sentence exceeds the budget — a hard cut. Cutting mid-sentence loses the clause that made
the passage meaningful, and a retrieved fragment starting "…which is therefore prohibited"
is worse than useless: it inverts.

**Overlap, deliberately.** Consecutive chunks share a tail so a fact spanning a boundary is
retrievable from either side. Without it, the one sentence that answers the question is
reliably the one split in half.

**Never emit an empty or whitespace-only chunk.** An empty chunk embeds to a vector that
matches everything weakly, which pollutes every search in the collection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.document_parsers import ParsedSegment

#: Paragraph break: a blank line, however much surrounding whitespace.
_PARAGRAPH = re.compile(r"\n\s*\n+")
#: Sentence end. Deliberately conservative — it requires the following character to be
#: whitespace, so "e.g." and "10.5" are not treated as boundaries.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

#: Roughly four characters per token, the same approximation used elsewhere. Exactness does
#: not matter; what matters is that a chunk sized for an embedding model's window actually
#: fits inside it.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str
    location: str | None = None

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // _CHARS_PER_TOKEN)


def chunk_segments(segments: list[ParsedSegment], *, chunk_size: int, overlap: int) -> list[Chunk]:
    """Turn parsed segments into embeddable chunks.

    Segments are chunked **independently**, never merged. A chunk spanning two pages could
    not honestly cite either, and a chunk spanning two CSV row-batches would join rows that
    have nothing to do with each other.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    # Overlap at or above chunk_size would never advance, so the loop would not terminate.
    overlap = max(0, min(overlap, chunk_size // 2))

    chunks: list[Chunk] = []
    for segment in segments:
        for text in _split(segment.text, chunk_size=chunk_size, overlap=overlap):
            cleaned = text.strip()
            if cleaned:
                chunks.append(Chunk(ordinal=len(chunks), text=cleaned, location=segment.location))
    return chunks


def _split(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # Paragraphs first. Packed greedily so a chunk holds as much whole-paragraph context as
    # fits, rather than one paragraph per chunk regardless of size.
    pieces: list[str] = []
    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= chunk_size:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_sentences(paragraph, chunk_size=chunk_size))

    return _pack(pieces, chunk_size=chunk_size, overlap=overlap)


def _split_sentences(paragraph: str, *, chunk_size: int) -> list[str]:
    """Break an over-long paragraph on sentence boundaries."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(paragraph):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > chunk_size:
            # A single sentence longer than the budget: a minified blob, a run-on table row,
            # or prose with no punctuation. Hard-cut on whitespace, which at least keeps
            # words intact.
            if current:
                out.append(current)
                current = ""
            out.extend(_hard_cut(sentence, chunk_size=chunk_size))
            continue
        if len(current) + len(sentence) + 1 <= chunk_size:
            current = f"{current} {sentence}".strip()
        else:
            out.append(current)
            current = sentence
    if current:
        out.append(current)
    return out


def _hard_cut(text: str, *, chunk_size: int) -> list[str]:
    out: list[str] = []
    remaining = text
    while len(remaining) > chunk_size:
        # Break at the last space inside the budget, so words survive. If there is no space
        # — a base64 blob, say — cut at the budget and accept it.
        cut = remaining.rfind(" ", 0, chunk_size)
        if cut <= 0:
            cut = chunk_size
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out


def _pack(pieces: list[str], *, chunk_size: int, overlap: int) -> list[str]:
    """Combine pieces into chunks, carrying an overlap tail between them."""
    chunks: list[str] = []
    current = ""

    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            chunks.append(current)
            # The tail of the chunk just emitted becomes the head of the next, so a fact
            # spanning the boundary is retrievable from either chunk.
            current = f"{_tail(current, overlap)}\n\n{piece}".strip() if overlap else piece
            # The tail plus this piece may itself overflow; emit the piece alone rather
            # than exceeding the budget the caller asked for.
            if len(current) > chunk_size:
                current = piece
        else:
            current = piece

    if current:
        chunks.append(current)
    return chunks


def _tail(text: str, overlap: int) -> str:
    """The last `overlap` characters, trimmed to a word boundary."""
    if overlap <= 0 or len(text) <= overlap:
        return text
    tail = text[-overlap:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail
