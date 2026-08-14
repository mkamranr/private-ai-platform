"""Knowledge bases, chunking, parsing and memory scoping (M15, M16).

Weighted towards **scoping** and **chunking**. The registry parts are bookkeeping; scoping
is where a mistake leaks one tenant's documents into another's answer, and chunking is
where retrieval quality is silently lost — both failures produce a fluent, confident, wrong
answer rather than an error.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.services.chunking import chunk_segments
from app.services.document_parsers import (
    ParsedSegment,
    UnsupportedFormatError,
    parse,
)
from app.services.memory import MemoryScope


# ---------------------------------------------------------------------------
# Parsing (§M15)
# ---------------------------------------------------------------------------
class TestParsing:
    def test_plain_text(self) -> None:
        result = parse("notes.txt", b"Hello there.\n\nSecond paragraph.")
        assert result.text_length > 0
        assert "Second paragraph" in result.segments[0].text

    def test_markdown_is_text(self) -> None:
        result = parse("readme.md", b"# Title\n\nBody text.")
        assert "Body text." in result.segments[0].text

    def test_csv_repeats_the_header_in_every_segment(self) -> None:
        """A retrieved fragment without its header is a grid of values whose columns
        nobody can identify."""
        rows = "\n".join(f"{i},row-{i},value-{i}" for i in range(200))
        result = parse("export.csv", f"id,name,value\n{rows}".encode())
        assert len(result.segments) > 1
        assert all(s.text.startswith("id, name, value") for s in result.segments)
        # Each segment says which rows it holds, so a citation is checkable.
        assert all(s.location and s.location.startswith("rows ") for s in result.segments)

    def test_html_drops_script_and_style(self) -> None:
        html = b"<html><head><style>p{color:red}</style></head><body><p>Visible.</p>"
        html += b"<script>alert('no')</script></body></html>"
        result = parse("page.html", html)
        text = result.segments[0].text
        assert "Visible." in text
        assert "alert" not in text
        assert "color:red" not in text

    def test_docx_without_lxml(self) -> None:
        """DOCX is a ZIP of XML, read with stdlib — no lxml in the air-gapped bundle."""
        document_xml = (
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Policy </w:t></w:r><w:r><w:t>statement.</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p></w:body></w:document>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)

        result = parse("policy.docx", buffer.getvalue())
        text = result.segments[0].text
        # Runs are joined without separators: Word splits a word across runs when
        # formatting changes mid-word, and inserting spaces would corrupt it.
        assert "Policy statement." in text
        assert "Second paragraph." in text

    def test_pptx_cites_the_slide(self) -> None:
        ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for number, body in ((1, "Opening"), (2, "Findings")):
                archive.writestr(
                    f"ppt/slides/slide{number}.xml",
                    f'<?xml version="1.0"?><p xmlns:a="{ns}"><a:t xmlns:a="{ns}">{body}</a:t></p>',
                )
        result = parse("deck.pptx", buffer.getvalue())
        assert [s.location for s in result.segments] == ["slide 1", "slide 2"]

    def test_an_image_needs_ocr_rather_than_failing(self) -> None:
        """Not a failure: nothing is wrong with the file, the platform cannot read it yet."""
        result = parse("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        assert result.needs_ocr is True
        assert result.segments == []

    def test_binary_is_refused(self) -> None:
        with pytest.raises(UnsupportedFormatError):
            parse("blob.bin", bytes(range(256)) * 20)

    def test_windows_1252_is_decoded_not_rejected(self) -> None:
        """Enterprise exports arrive as cp1252 more often than anyone would like."""
        result = parse("legacy.txt", "Ma\xf1ana — caf\xe9".encode("cp1252"))
        assert "ana" in result.segments[0].text


# ---------------------------------------------------------------------------
# Chunking (§M15)
# ---------------------------------------------------------------------------
class TestChunking:
    def test_short_text_is_one_chunk(self) -> None:
        chunks = chunk_segments(
            [ParsedSegment(text="A short policy statement.")], chunk_size=500, overlap=50
        )
        assert len(chunks) == 1
        assert chunks[0].ordinal == 0

    def test_respects_the_size_budget(self) -> None:
        text = " ".join(f"Sentence number {i} about leave policy." for i in range(400))
        chunks = chunk_segments([ParsedSegment(text=text)], chunk_size=400, overlap=50)
        assert len(chunks) > 1
        assert all(len(c.text) <= 400 for c in chunks), [len(c.text) for c in chunks]

    def test_consecutive_chunks_overlap(self) -> None:
        """Without overlap, the one sentence that answers the question is reliably the one
        split across a boundary."""
        paragraphs = [f"Paragraph {i} with several words in it." for i in range(40)]
        chunks = chunk_segments(
            [ParsedSegment(text="\n\n".join(paragraphs))], chunk_size=300, overlap=100
        )
        assert len(chunks) > 2
        # Some content from chunk N reappears at the head of chunk N+1.
        shared = [
            any(word in chunks[i + 1].text for word in chunks[i].text.split()[-4:])
            for i in range(len(chunks) - 1)
        ]
        assert any(shared), "no overlap between any consecutive chunks"

    def test_never_emits_an_empty_chunk(self) -> None:
        """An empty chunk embeds to a vector that weakly matches everything, polluting
        every search in the collection."""
        chunks = chunk_segments(
            [ParsedSegment(text="\n\n\n   \n\n Real content here. \n\n\n   ")],
            chunk_size=100,
            overlap=10,
        )
        assert all(c.text.strip() for c in chunks)
        assert len(chunks) == 1

    def test_segments_are_never_merged(self) -> None:
        """A chunk spanning two pages could not honestly cite either."""
        chunks = chunk_segments(
            [
                ParsedSegment(text="Page one content.", location="page 1"),
                ParsedSegment(text="Page two content.", location="page 2"),
            ],
            chunk_size=5000,
            overlap=100,
        )
        assert len(chunks) == 2
        assert [c.location for c in chunks] == ["page 1", "page 2"]

    def test_a_single_over_long_sentence_is_hard_cut_on_words(self) -> None:
        text = " ".join(["word"] * 500)
        chunks = chunk_segments([ParsedSegment(text=text)], chunk_size=200, overlap=20)
        assert all(len(c.text) <= 200 for c in chunks)
        # Words survive the cut.
        assert all("wor d" not in c.text for c in chunks)

    def test_overlap_at_or_above_chunk_size_cannot_hang(self) -> None:
        """A caller passing overlap >= chunk_size would otherwise never advance."""
        chunks = chunk_segments([ParsedSegment(text="x " * 2000)], chunk_size=100, overlap=500)
        assert len(chunks) > 1

    def test_ordinals_are_contiguous(self) -> None:
        chunks = chunk_segments(
            [ParsedSegment(text="\n\n".join(f"Para {i}." for i in range(60)))],
            chunk_size=200,
            overlap=20,
        )
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# Memory scoping (§M16) — the security core of this phase
# ---------------------------------------------------------------------------
class TestMemoryScope:
    def test_a_scope_must_name_a_tenant(self) -> None:
        """An empty tenant matches nothing or everything depending on the store, and
        neither is acceptable."""
        with pytest.raises(ValidationError):
            MemoryScope(tenant_id="")

    def test_filters_always_carry_the_tenant(self) -> None:
        filters = MemoryScope(tenant_id="finance").vector_filters()
        assert filters["tenant_id"] == "finance"

    def test_none_values_are_left_for_the_store_to_drop(self) -> None:
        """`user_id=None` means "not scoped to a user", not "match null" — which is what
        makes an agent-agnostic recall expressible without loosening the tenant boundary."""
        filters = MemoryScope(tenant_id="t", end_user="a@b.local").vector_filters()
        assert filters["user_id"] is None
        assert filters["end_user"] == "a@b.local"

    def test_anonymous_scope_is_recognised(self) -> None:
        assert MemoryScope(tenant_id="t").is_anonymous is True
        assert MemoryScope(tenant_id="t", end_user="a@b").is_anonymous is False
        assert MemoryScope(tenant_id="t", user_id=uuid.uuid4()).is_anonymous is False

    def test_user_and_end_user_are_distinct_dimensions(self) -> None:
        """A platform account and a chat identity behind a shared frontend are different
        things (M17). Collapsing them would either lose per-person memory in chat, or treat
        an unauthenticated string as a platform identity."""
        user_scope = MemoryScope(tenant_id="t", user_id=uuid.uuid4())
        chat_scope = MemoryScope(tenant_id="t", end_user="someone@corp.local")
        assert user_scope.vector_filters() != chat_scope.vector_filters()


class TestVectorFilterTranslation:
    def test_untranslatable_filters_raise_rather_than_search_unfiltered(self) -> None:
        """**The most important test here.** Silently dropping a filter turns a
        tenant-scoped search into a global one, and the caller gets a plausible answer built
        from another tenant's documents."""
        from app.services.vector_store import _to_filter

        with pytest.raises(TypeError, match="Refusing rather than searching unfiltered"):
            _to_filter({"tenant_id": {"nested": "object"}})

    def test_none_values_are_dropped_not_matched(self) -> None:
        from app.services.vector_store import _to_filter

        translated = _to_filter({"tenant_id": "acme", "user_id": None})
        assert translated is not None
        assert len(translated.must) == 1  # type: ignore[arg-type]

    def test_empty_filters_are_none(self) -> None:
        from app.services.vector_store import _to_filter

        assert _to_filter({}) is None
        assert _to_filter(None) is None

    async def test_delete_without_a_selector_is_refused(self) -> None:
        """One forgotten argument away from emptying a collection."""
        from app.services.vector_store import QdrantVectorStore

        store = QdrantVectorStore(client=_FakeQdrant())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="refusing to delete a whole collection"):
            await store.delete("some-collection")


class _FakeQdrant:
    async def collection_exists(self, collection_name: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# Knowledge base API (M15)
# ---------------------------------------------------------------------------
class TestKnowledgeBaseApi:
    async def test_creating_requires_knowledge_manage(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        from app.core.permissions import Permission as Perm
        from tests.api.conftest import _user_with
        from tests.conftest import auth_header

        viewer = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="kbviewer")
        response = await client.post(
            "/api/v1/knowledge-bases",
            headers=auth_header(tokens, viewer),
            json={
                "name": "denied-base",
                "display_name": "Denied",
                "embedding_model": "enterprise-embed",
            },
        )
        assert response.status_code == 403

    async def test_name_is_validated(self, client, tokens, session: AsyncSession, settings) -> None:
        from app.core.permissions import Permission as Perm
        from tests.api.conftest import _user_with
        from tests.conftest import auth_header

        admin = await _user_with(session, settings, [Perm.KNOWLEDGE_MANAGE], name="kbadmin")
        response = await client.post(
            "/api/v1/knowledge-bases",
            headers=auth_header(tokens, admin),
            json={
                "name": "Not A Valid Name",
                "display_name": "x",
                "embedding_model": "enterprise-embed",
            },
        )
        assert response.status_code == 422

    async def test_overlap_must_be_smaller_than_chunk_size(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        """Otherwise chunking never advances."""
        from app.core.permissions import Permission as Perm
        from tests.api.conftest import _user_with
        from tests.conftest import auth_header

        admin = await _user_with(session, settings, [Perm.KNOWLEDGE_MANAGE], name="kbadmin2")
        response = await client.post(
            "/api/v1/knowledge-bases",
            headers=auth_header(tokens, admin),
            json={
                "name": f"bad-overlap-{uuid.uuid4().hex[:6]}",
                "display_name": "x",
                "embedding_model": "enterprise-embed",
                "chunk_size": 500,
                "chunk_overlap": 500,
            },
        )
        assert response.status_code == 422

    async def test_memory_search_refuses_an_anonymous_scope(
        self, client, tokens, session: AsyncSession, settings
    ) -> None:
        """This endpoint must not become the unfiltered search the rest of the module
        refuses to perform."""
        from app.core.permissions import Permission as Perm
        from tests.api.conftest import _user_with
        from tests.conftest import auth_header

        viewer = await _user_with(session, settings, [Perm.KNOWLEDGE_VIEW], name="memviewer")
        response = await client.post(
            "/api/v1/memory/search",
            headers=auth_header(tokens, viewer),
            json={"query": "anything", "tenant_id": "default"},
        )
        assert response.status_code == 422
        assert "user_id or end_user" in response.json()["error"]["message"]
