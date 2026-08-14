# Knowledge, RAG and memory (M15, M16)

```
upload → parse → OCR if required → chunk → embed → Qdrant
```

Ingestion runs in a **worker**, never a request: parsing and embedding a 200-page PDF takes
minutes. `POST /knowledge-bases/{id}/documents` returns **202** and the caller polls — the
same reasoning as model deployment (M08).

---

## Formats

| | Parser | Cites |
|---|---|---|
| PDF | `pypdf` | page |
| DOCX | stdlib `zipfile` + `xml.etree` | — |
| PPTX | stdlib `zipfile` + `xml.etree` | slide |
| CSV | stdlib `csv`, header repeated per batch | row range |
| HTML | stdlib `HTMLParser`, script/style dropped | — |
| TXT · MD · JSON · YAML · log | stdlib | — |
| Images, scanned PDFs | **OCR** (M28) | `enterprise-ocr` |

**Only PDF needs a dependency.** DOCX and PPTX are ZIP archives of XML, so they are read
with stdlib rather than `python-docx`/`python-pptx` — both of which pull `lxml`, a C
extension that then has to build for the air-gapped target's architecture, to gain layout
fidelity that text extraction never uses.

Each parser returns `(text, location)` pairs, so a chunk can cite a page or slide. A
citation nobody can check is not much better than no citation.

**A scanned file becomes `NO_TEXT`, not `FAILED`.** Nothing is wrong with it; the platform
cannot read it yet. The status detail says so and names Phase 9. Marking it FAILED would
send an operator hunting a bug that does not exist.

Non-UTF-8 exports (cp1252 is depressingly common) are decoded rather than rejected.

---

## Chunking

Where retrieval quality is mostly won or lost, and the failure is silent: badly chunked
text still embeds, still searches, still returns hits — just the wrong ones, or fragments
too mutilated to use.

* **Structure before length.** Paragraph → sentence → hard cut on whitespace. A fragment
  beginning "…which is therefore prohibited" does not merely lose context, it inverts.
* **Overlap between consecutive chunks.** Without it, the one sentence that answers the
  question is reliably the one split across a boundary.
* **Never an empty chunk.** An empty chunk embeds to a vector that weakly matches
  everything, polluting every search in the collection.
* **Segments are never merged.** A chunk spanning two pages could not honestly cite either.

Chunk size and overlap are **per knowledge base**: policy prose wants large chunks with
context, a FAQ wants small sharp ones.

---

## Storage: text here, vectors there

`document_chunks.text` in PostgreSQL is authoritative. Qdrant holds the embedding plus a
small payload — scope, document name, ordinal, and a 200-character preview.

That split buys three things:

1. **Re-embedding is a rebuild, not a re-ingest.** A new model or chunk size rebuilds from
   rows the platform already holds, without the source files.
2. **A citation always resolves**, even while the vector store is being rebuilt.
3. **Search answers from the full text**, never the preview — answering from a truncated
   payload would silently degrade every answer.

The original file is kept in MinIO too, so a re-embed with a *better parser* is possible
without whoever uploaded it.

The Qdrant collection is named from the base's **id**, not its name: renaming a knowledge
base must not orphan its vectors.

---

## Scoping (§M16) — the security core

**Every search is scoped, and the vector store raises rather than searching unfiltered.**

```python
filters = {"tenant_id": ..., "knowledge_base_id": ..., "user_id": ..., "agent_id": ...}
```

`_to_filter` refuses anything it cannot express. That is deliberate: silently dropping a
filter turns a tenant-scoped search into a global one, and the caller receives a
plausible-looking answer built from another tenant's documents. There is no error, no
exception, no log line — just a confident wrong answer. Failing loudly is the only safe
behaviour.

`MemoryScope` is a required argument on every memory operation, not a set of optional
keyword arguments, so no call site can omit it. It carries **both** `user_id` and
`end_user`: a platform account and a chat identity behind a shared frontend (M17) are
different things, and collapsing them would either lose per-person memory in chat or treat
an unauthenticated string as a platform identity.

Writing a memory under an anonymous scope is **refused** — it would be recallable by
everyone in the tenant, which is the same leak arriving through the write path.

Deleting vectors is by **id**, never by filter: a filter is one typo from matching more
than intended, and the mistake is invisible until an answer cites a document that was
supposed to be gone. `VectorStore.delete()` with neither ids nor filters raises rather than
being read as "delete everything".

---

## Three memory layers (§M16)

| Layer | Store | Holds | Lifetime |
|---|---|---|---|
| `SESSION` | Valkey | Working state in one conversation | TTL, expires itself |
| `LONG_TERM` | PostgreSQL | Conversation history and a rolling summary | Until deleted |
| `SEMANTIC` | Qdrant | Facts and exchanges, recalled by similarity | Until expiry or deletion |

The §M16 API: `store_memory` → `remember()`, `search_memory` → `recall()`,
`get_conversation()`, `summarize_conversation()`, `delete_memory` → `forget()`.

Semantic memory uses **one collection partitioned by payload filters**, not a collection
per user. Thousands of tiny collections each carry their own index, and a scope change — a
user moving tenant — would become a data migration. Filters are enforced by `MemoryScope`,
so the partitioning is not the safety mechanism.

Summarising is necessary rather than nice: recall that replays every message grows without
bound and eventually will not fit the context window. The old part is folded into a
summary and the recent part kept verbatim, because a follow-up question usually depends on
what was just said.

`forget_all()` erases across **all three layers**. A partial erasure would be worse than
none: the platform would report the data as deleted while still recalling it.

Session keys are swept with `SCAN`, never `KEYS` — `KEYS` blocks Valkey for the length of
the scan, which on a shared cache blocks every other request on the platform.

---

## Retrieval in an agent run

**Injected as context, not offered as a tool.** An agent given a "search the documents" tool
routinely answers without calling it, and the failure looks like the documents being
missing rather than the model choosing not to look. Injecting removes the choice.

The context block is inserted as a **system message at the head** of the conversation:
appending it after the question leaves the model reading the question first and the
evidence second, which measurably worsens grounding. It names each passage's source and
instructs the model to say when the context is insufficient — the instruction that stops
retrieval turning into confident invention.

`RAG_SEARCH` is emitted **even when nothing matches**. "We searched these bases and found
nothing" is precisely what someone investigating a wrong answer needs; a missing event is
indistinguishable from retrieval never having been attempted. Payloads carry citations, not
text — an event readable by anyone with `agent.view` should not become a second copy of the
corpus.

Cross-tenant knowledge base references on an agent are **dropped and logged**, not searched:
a stale or hand-edited id must not become a path into another tenant's corpus.

Retrieval never raises. A vector store outage degrades the answer; it does not refuse it.

---

## Embedding models

A knowledge base's embedding model is **fixed once it holds chunks**. Vectors from two
models are not comparable, so mixing them produces search results that are quietly
meaningless rather than obviously broken.

**Dimensions are discovered, not configured** — by embedding one probe string at creation
time. A configured number that was wrong would create a collection rejecting every vector,
with nothing to tell the operator the right one.

Embedding goes **through the gateway**, so a knowledge base uses the same alias resolution
and the same deployment a developer's API call would (§13). A second path would eventually
disagree about which model is serving, and the symptom would be a base whose vectors are
incomparable with its own queries.

### The mock embedding model

`mock-embed` (alias `enterprise-embed`) serves **hashed bag-of-words** vectors: each token
hashes to a dimension, weighted sub-linearly, then L2-normalised.

Crude, and deliberately not random. A random-vector mock — the obvious one — makes every
cosine similarity land near zero, so any sensible relevance floor filters out everything and
retrieval cannot be tested at all: a search returns nothing whether the pipeline works or
not. With lexical overlap, a passing retrieval test means the ingest → embed → index →
search → cite path genuinely works.

It is still not semantic: it cannot match "holiday" to "annual leave". A real embedding
model is needed for retrieval **quality**; this one gives retrieval **correctness**.

---

## Orphaned containers

Not strictly Phase 5, but found here. A container the platform created but has no record of
holds GPUs, serves a model nobody can see, and survives every restart. It appears when:

* the control plane dies between creating the container and committing the row;
* a database is restored to a point before the deployment;
* someone runs `alembic downgrade base` — which is how the acceptance gates produced twelve
  of them before this was noticed.

```bash
make reconcile              # report
make reconcile REMOVE=1     # remove
```

Also `POST /api/v1/deployments/reconcile?remove=true`, and the deployment worker reports
(never removes) every ~10 minutes. Only containers carrying the platform's managed label are
ever considered — the same guard that stops the platform stopping its own database.

---

## Known gaps

* **OCR runs when an OCR model is deployed** (M28). A scanned page goes
  `PARSING → OCR → CHUNKING → …` and ends up indexed with its page numbers intact, so a
  chunk from a scan cites "page 3" exactly as one from a PDF does. With nothing serving
  `enterprise-ocr`, the document lands in `NO_TEXT` with a reason naming what to deploy —
  not `FAILED`, because "there is no text in this file yet" is a different problem from
  "the platform broke".

  The engine contract is small on purpose, because OCR has no OpenAI-style standard: the
  platform posts a multipart image to `POST /ocr` and expects
  `{"blocks": [{"text", "page", "confidence"}], "language"}`. `mock-vllm` implements it
  for GPU-free development; `models/manifests/examples/paddleocr-v4.yaml` is the
  production shape.
* **No re-embed endpoint.** Changing a base's model or chunk size needs deleting and
  re-uploading; the data to do it in place is all there (`document_chunks.text` plus the
  stored original) but the operation is not exposed.
* **Nothing sweeps expired memories.** `delete_expired` exists and is tested; recall skips
  expired entries at read time, so they are inert, but they are not reclaimed.
* **Agents do not write memory yet.** `remember()` works and is scoped, but no run calls it
  — what an agent *should* choose to remember is a product decision, and inferring it
  silently is how an agent's picture of someone ossifies around a guess.
* **`DATABASE` and `OPENAPI` tools remain unimplemented** (Phase 4 gap, unchanged).

---

## In the admin UI

**Knowledge → Knowledge Bases** creates a base, uploads documents, and shows each one
walking the §M15 lifecycle (`PENDING → PARSING → CHUNKING → EMBEDDING → INDEXED`) as the
worker gets to it. The page polls, so an ingesting document updates in place.

Only aliases pointing at a **serving EMBEDDING model** are offered when creating a base.
A chat model produces something vector-shaped and the collection is created happily; the
failure then surfaces much later as retrieval that finds nothing, a long way from the form
that caused it.

**Retrieval preview** runs a search against the base and shows the passages with their
scores — the text an agent would actually be given. When an agent answers badly the first
question is whether retrieval found the right passages, and this answers it without running
an agent at all. Zero hits render as a warning rather than an error, because "nothing
matched" and "the search broke" send an operator to different places.

**Knowledge → Memory** lists what the platform remembers about one subject, across all
three layers, and erases it. Every read is scoped: there is no whole-tenant view, because
that view is precisely one person's memories shown to another. **Erase subject** requires a
subject for the same reason — the endpoint would accept a tenant-wide erasure, and the UI
does not offer it.

Expired memories are shown dimmed rather than hidden. Recall skips them, but nothing sweeps
them yet, so they are still stored — and an erasure request is answered by what is stored,
not by what is recalled.
