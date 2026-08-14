"""Document text extraction (§M15).

    upload → **parse** → OCR if required → chunk → embed → Qdrant

Formats: TXT, MD, HTML, CSV, JSON, PDF, DOCX, PPTX. Images are recognised and routed to
the OCR seam, which is unimplemented until Phase 9 — a scanned page therefore parses to
nothing and the document lands in ``NO_TEXT`` rather than failing, because "there is no
text in this file yet" is a different problem from "the platform broke".

**Only PDF needs a dependency.** DOCX and PPTX are ZIP archives of XML, read here with
stdlib ``zipfile`` and ``xml.etree``. The alternative, python-docx and python-pptx, both
pull ``lxml`` — a C extension that must then build for the air-gapped target's
architecture, to gain layout fidelity that text extraction does not use.

Every parser returns ``(text, location)`` pairs rather than one string, so a chunk can
cite a page or slide. A citation nobody can check is not much better than no citation.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.etree import ElementTree

from app.core.logging import get_logger

log = get_logger(__name__)

#: Extensions that hold an image rather than text. Recognised so they can be routed to OCR
#: and reported honestly, rather than parsed to an empty string and called indexed.
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"})


@dataclass(slots=True)
class ParsedSegment:
    """A piece of extracted text and where it came from."""

    text: str
    #: "page 4", "slide 2", "row 1-500" — whatever the format can say. None when it
    #: genuinely cannot: a flat text file has no meaningful location.
    location: str | None = None


@dataclass(slots=True)
class ParseResult:
    segments: list[ParsedSegment] = field(default_factory=list)
    #: True when the source is an image and text could only come from OCR.
    needs_ocr: bool = False
    #: Set when parsing partially failed. The document still indexes what was recovered —
    #: one corrupt page in a 200-page report should not discard the other 199.
    warning: str | None = None

    @property
    def text_length(self) -> int:
        return sum(len(s.text) for s in self.segments)


class UnsupportedFormatError(ValueError):
    pass


def parse(filename: str, content: bytes, content_type: str = "") -> ParseResult:
    """Extract text from an uploaded file.

    Dispatches on extension first and content type second: a browser's guess at the type
    of a `.md` file is routinely `application/octet-stream`, and the extension is what the
    person who uploaded it actually chose.
    """
    lower = filename.lower()
    suffix = lower[lower.rfind(".") :] if "." in lower else ""

    if suffix in IMAGE_EXTENSIONS or content_type.startswith("image/"):
        return ParseResult(needs_ocr=True)

    if suffix == ".pdf" or content_type == "application/pdf":
        return _parse_pdf(content)
    if suffix == ".docx":
        return _parse_docx(content)
    if suffix == ".pptx":
        return _parse_pptx(content)
    if suffix in (".html", ".htm") or content_type.startswith("text/html"):
        return _parse_html(content)
    if suffix == ".csv" or content_type == "text/csv":
        return _parse_csv(content)
    if suffix == ".json" or content_type == "application/json":
        return _parse_json(content)
    if suffix in (".txt", ".md", ".markdown", ".log", ".rst", ".yaml", ".yml") or (
        content_type.startswith("text/")
    ):
        return _parse_text(content)

    # Last resort: if it decodes as UTF-8 and looks like prose, treat it as text. Better
    # than refusing a `.conf` or an extensionless export the operator knows is readable.
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedFormatError(
            f"{filename}: not a supported format, and not decodable as UTF-8 text."
        ) from exc
    if _looks_binary(decoded):
        raise UnsupportedFormatError(f"{filename}: decodes as text but looks binary.")
    return ParseResult(segments=[ParsedSegment(text=decoded)], warning="Parsed as plain text.")


# ---------------------------------------------------------------------------
def _parse_text(content: bytes) -> ParseResult:
    return ParseResult(segments=[ParsedSegment(text=_decode(content))])


def _parse_pdf(content: bytes) -> ParseResult:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
    except (PdfReadError, OSError, ValueError) as exc:
        raise UnsupportedFormatError(f"Could not read the PDF: {exc}") from exc

    segments: list[ParsedSegment] = []
    failed_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            # One bad page does not discard the document.
            failed_pages += 1
            continue
        if text.strip():
            segments.append(ParsedSegment(text=text, location=f"page {number}"))

    warning = None
    if failed_pages:
        warning = f"{failed_pages} page(s) could not be read and were skipped."
    if not segments:
        # A PDF of scanned images. Reported as needing OCR rather than as a failure —
        # nothing is wrong with the file, the platform simply cannot read it yet.
        return ParseResult(
            needs_ocr=True,
            warning="No extractable text; this looks like a scanned PDF and needs OCR.",
        )
    return ParseResult(segments=segments, warning=warning)


#: OOXML namespaces. Hard-coded because they are fixed by the standard, and looking them
#: up dynamically would only add a way to get them wrong.
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _parse_docx(content: bytes) -> ParseResult:
    """Extract paragraphs from `word/document.xml`.

    A DOCX is a ZIP of XML. Walking it with stdlib avoids lxml entirely; the cost is that
    tables come out as their cell text in document order, which for retrieval is fine.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise UnsupportedFormatError(f"Not a readable DOCX: {exc}") from exc

    root = ElementTree.fromstring(xml)  # noqa: S314 — OOXML from an authenticated upload
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        # `w:t` runs within a paragraph are joined without separators: Word splits a single
        # sentence across runs whenever formatting changes mid-word.
        text = "".join(node.text or "" for node in paragraph.iter(f"{_W_NS}t"))
        if text.strip():
            paragraphs.append(text)

    if not paragraphs:
        return ParseResult(warning="The document contained no paragraph text.")
    return ParseResult(segments=[ParsedSegment(text="\n\n".join(paragraphs))])


def _parse_pptx(content: bytes) -> ParseResult:
    """Extract text per slide, so a citation can name the slide."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            slide_names = sorted(
                (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.findall(r"\d+", n)[-1]),
            )
            if not slide_names:
                raise UnsupportedFormatError("Not a readable PPTX: no slides found.")

            segments: list[ParsedSegment] = []
            for index, name in enumerate(slide_names, start=1):
                root = ElementTree.fromstring(archive.read(name))  # noqa: S314
                text = "\n".join(
                    node.text for node in root.iter(f"{_A_NS}t") if node.text and node.text.strip()
                )
                if text.strip():
                    segments.append(ParsedSegment(text=text, location=f"slide {index}"))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormatError(f"Not a readable PPTX: {exc}") from exc

    if not segments:
        return ParseResult(warning="The presentation contained no text (images only?).")
    return ParseResult(segments=segments)


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script and style content.

    stdlib rather than BeautifulSoup: one dependency fewer in the bundle, and the job is
    "give me the words", not "understand the DOM".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            # Block boundaries become newlines, so the chunker can split on paragraphs
            # rather than mid-sentence.
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)


def _parse_html(content: bytes) -> ParseResult:
    extractor = _TextExtractor()
    extractor.feed(_decode(content))
    text = re.sub(r"\n{3,}", "\n\n", " ".join(extractor.parts).replace(" \n ", "\n"))
    if not text.strip():
        return ParseResult(warning="The HTML contained no visible text.")
    return ParseResult(segments=[ParsedSegment(text=text.strip())])


#: Rows per CSV segment. A 50,000-row export as one blob would embed to a single vector
#: that means nothing; per row would produce 50,000 near-identical vectors. Batching keeps
#: each chunk a readable table fragment with its header.
_CSV_ROWS_PER_SEGMENT = 40


def _parse_csv(content: bytes) -> ParseResult:
    reader = csv.reader(io.StringIO(_decode(content)))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise UnsupportedFormatError(f"Could not read the CSV: {exc}") from exc
    if not rows:
        return ParseResult(warning="The CSV was empty.")

    header, *body = rows
    if not body:
        return ParseResult(segments=[ParsedSegment(text=", ".join(header))])

    segments: list[ParsedSegment] = []
    for start in range(0, len(body), _CSV_ROWS_PER_SEGMENT):
        batch = body[start : start + _CSV_ROWS_PER_SEGMENT]
        # The header is repeated in every segment. Without it a retrieved fragment is a
        # grid of values whose columns nobody can identify.
        lines = [", ".join(header)] + [", ".join(cell for cell in row) for row in batch]
        segments.append(
            ParsedSegment(
                text="\n".join(lines),
                location=f"rows {start + 1}-{start + len(batch)}",
            )
        )
    return ParseResult(segments=segments)


def _parse_json(content: bytes) -> ParseResult:
    try:
        parsed = json.loads(_decode(content))
    except ValueError as exc:
        raise UnsupportedFormatError(f"Not valid JSON: {exc}") from exc
    # Re-serialised with indentation: a minified blob is one enormous line, which chunks
    # badly and reads worse when it comes back as a citation.
    return ParseResult(segments=[ParsedSegment(text=json.dumps(parsed, indent=2))])


# ---------------------------------------------------------------------------
def _decode(content: bytes) -> str:
    """Decode text, tolerating a non-UTF-8 export.

    Enterprise documents arrive as Windows-1252 more often than anyone would like, and
    refusing them outright helps nobody. Replacement characters are preferable to a
    rejected upload of an otherwise readable file.
    """
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _looks_binary(text: str) -> bool:
    sample = text[:4000]
    if not sample:
        return False
    # NUL is the giveaway; a high proportion of unprintables is the fallback signal.
    if "\x00" in sample:
        return True
    unprintable = sum(1 for c in sample if not c.isprintable() and c not in "\n\r\t")
    return unprintable / len(sample) > 0.10
