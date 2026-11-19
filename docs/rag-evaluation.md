# Retrieval depth: reranking, versions and evaluation

This page covers three additions to the knowledge bases of [rag.md](rag.md):

* an optional **rerank stage** after vector search, configured per knowledge base;
* **document versions**, so a document can be replaced without a gap in what it answers;
* a **retrieval playground** and **evaluation sets** that measure retrieval.

```
query → embed → vector search (candidates) → rerank (optional) → top-k → agent context
```

Upgrading changes no behaviour. Migration `5f2b8c1d4a37` gives every existing base no
reranker, top-k 4 and 20 candidates (what agents already retrieved), and makes every
existing document version 1 of itself, current.

---

## Retrieval settings

Each knowledge base has four settings, changed with `PATCH /knowledge-bases/{id}`
(`knowledge.manage`) or the **Settings** button on the Knowledge Bases page:

| setting | default | meaning |
|---|---|---|
| `reranker_model` | none | A `RERANKER` model or alias. None: vector order is final. |
| `retrieval_top_k` | 4 | Passages returned per search, 1–50. |
| `rerank_candidates` | 20 | Vector candidates handed to the reranker, 1–200, at least top-k. Used only with a reranker. |
| `min_score` | none | Minimum vector similarity. None uses the caller's floor (agents: 0.25). |

Agents use these settings on every run. The search endpoint, the playground and evaluation
runs can override them per request.

The reranker is **validated when it is set**. It must name a model (or an enabled alias)
that the caller can see, and its type must be `RERANKER`. Otherwise a wrong model would
only show up at the first search, as a warning that results fell back to vector order.

The embedding model and chunking cannot change in a PATCH. Changing either changes what
the stored vectors mean, which needs a rebuild, not an edit.

### How reranking runs

1. Each base is searched in its own collection for `rerank_candidates` passages above
   the score floor. Candidates are merged by cosine similarity.
2. The reranker scores every candidate against the query in one call. Results are sorted
   by that score and the first `top_k` are returned.
3. `score` on a result is always the **vector similarity**. The reranker's score is a
   separate `rerank_score`, and `vector_rank` records where the passage stood before
   reranking. A score's meaning does not change with configuration.

A reranker can only promote passages that vector search found. If the right passage is
not among the candidates, raise `rerank_candidates`. Don't expect the reranker to find it.

**Failure never fails a retrieval.** If the reranker is unreachable, errors, or does not
score every passage, the results stay in vector order and the trace carries a warning. An
agent run logs it as `agent_retrieval_degraded`. A reranker is an optional stage, and an
outage of it should degrade answers, not refuse them.

**Several bases, different rerankers:** scores from two cross-encoders are not
comparable, so a merged list could not be ordered honestly. If the bases configure
different rerankers, none is applied and the trace says so. Choose one explicitly
(`reranker: "model"`) to rerank them together.

### Serving a reranker

Rerank calls go through the gateway's model resolution (`provider_for_model`), like
embedding and OCR, so aliases and deployments behave identically. Two wire formats are
supported, and both belong to local engines:

* vLLM `POST /v1/rerank` (Jina/Cohere shape): `{model, query, documents}` returns
  `results[].index` and `relevance_score`. Tried first.
* Hugging Face TEI `POST /rerank`: `{query, texts}` returns `[{index, score}]`. Used when
  the first answers 404.

`mock-vllm` implements `/v1/rerank` for development. It scores by query-term coverage,
which is lexical, not semantic, for the same reason the mock embedder is (see
[rag.md](rag.md#the-mock-embedding-model)).

Register the model with type `RERANKER`, deploy it, and give it an alias such as
`enterprise-rerank`. For example, BAAI `bge-reranker-v2-m3` on vLLM
(`--task score`) or TEI.

---

## Document versions

Every version of a document shares a `lineage_id`, which is the first version's id. Only
the **current** version is searched.

| action | route | result |
|---|---|---|
| Upload a new version | `POST /documents/{id}/versions` (`document.upload`, multipart) | **202**. Next version number, not current yet. |
| List versions | `GET /documents/{id}/versions` | Newest first. |
| Re-ingest | `POST /documents/{id}/reingest` (`document.upload`) | **202**. Back to `UPLOADED` from the stored original. |
| Delete | `DELETE /documents/{id}` | Current version: deletes every version. Superseded version: only that one. |

**No gap when replacing.** A new version is ingested like any upload, but the version it
replaces keeps answering until the new one reaches `INDEXED`. The swap happens then, in
the same transaction as the indexing: the old version's vectors and chunks are removed,
and it becomes superseded (`is_current: false`, `superseded_at` set). Its record and stored
original are kept as history. If the new version fails or ends `NO_TEXT`, nothing changes:
the old version stays current.

While the new version is being embedded, its vectors already exist in the collection.
Search skips chunks whose document is not current, so they are never returned early.

Refused with **409**: a version identical (same SHA-256) to any version of the document,
and a new version while another is still being ingested.

**Re-ingest is in place.** It suits a chunking fix, a newly deployed OCR model, or a
failure that has since been resolved. The document is not searchable until it is indexed
again. To change content without that gap, upload a version instead. Superseded and
in-flight documents cannot be re-ingested (409).

Re-chunking **removes the previous vectors** before writing new ones, so a retried
ingestion does not leave old points in the collection as orphans.

`GET /knowledge-bases/{id}/documents` hides superseded versions unless
`?include_superseded=true`.

### Index health

`GET /knowledge-bases/{id}` reports `chunks` (points in the vector collection) and
`stored_chunks` (chunk rows of current documents). They are equal when the index is
healthy. While a document is embedding they briefly differ. A lasting gap means vectors
are missing, from a partial failure, or orphaned, and the console shows a warning on the
base. Re-ingesting the affected documents rebuilds them.

---

## Retrieval playground

`POST /retrieval/playground` (`knowledge.view`) runs one query against up to ten
knowledge bases and returns every stage:

```json
{
  "knowledge_base_ids": ["…"],
  "query": "How many leave days carry over?",
  "top_k": 3,
  "candidates": 20,
  "score_threshold": null,
  "reranker": "default",
  "reranker_model": null,
  "compare": true,
  "answer_model": "enterprise-chat",
  "judge_model": "enterprise-judge"
}
```

`reranker` is `default` (each base's setting), `none` (vector order), or `model` (the
`RERANKER` named in `reranker_model`). Omitted numbers use the bases' settings.

The response holds `run`, with `candidates` in vector order, the final `results`, the
reranker used, `timings_ms` per stage (`embed`, `search`, `rerank`; measured wall-clock,
summed across bases) and `warnings`. With `compare`, and when a reranker ran, it also holds
`baseline`: the same query in vector order.

In the console, **Knowledge → Retrieval Playground** shows the reranked and vector-order
results side by side. Each passage shows how far the reranker moved it ("▲ 2 from #3").
The full candidate table shows which candidates were dropped.

### The answer, and how faithful it is

Retrieval metrics say whether the right documents came back. They can't say whether the
answer a model then wrote stayed inside them, and that is the failure people report: a
confident answer citing a policy that says something else.

* **`answer_model`** writes the answer from the final results, with exactly the context
  block an agent gets (numbered passages, the instruction to cite and to say when the
  passages don't cover it). The response's `answer` holds the text, model and latency.
* **`judge_model`** (needs `answer_model`) splits that answer into claims and checks each
  against the numbered passages. `verdict.faithfulness` is the share of claims the passages
  support; `verdict.claims` lists each claim, whether it is supported and by which
  passages. An answer that makes no claims ("the documents don't cover this") has
  `faithfulness: null`, not 100%, so refusals can't inflate a score.
* A model that isn't serving, a used-up budget, or a judge reply that isn't the JSON asked
  for is reported in `answer_error` or `judge_error`. The passages are still returned.
* Both calls go through the gateway like every platform call: alias resolution, scope
  (the caller's teams must be able to use the model), the team's and model's budgets, and
  a usage row (`retrieval.answer`, `retrieval.judge`) charged to the bases' team.

The judge is a model and can be wrong. Its claims are shown so a person can check the
verdict, not only the number. Use a capable model as the judge, ideally not the one that
wrote the answer.

### Question history

Every playground question is saved for `knowledge.query_history_days` (default 30; 0
keeps none), with its settings, the final passages (text shortened to 1,200 characters),
the answer and the verdict. An hourly job deletes older questions.

| method | path | permission |
|---|---|---|
| GET | `/retrieval/history` | `knowledge.view`. Your questions, newest first. `knowledge_base_id` filters; `everyone=true` with a `knowledge_base_id` lists everyone's questions to that base (`knowledge.manage`); `before`, `limit` page |
| GET | `/retrieval/history/{id}` | `knowledge.view`. One question |
| POST | `/retrieval/history/{id}/replay` | `knowledge.view`. The same question, settings and models against today's documents; saved as a new question (`replay_of`). `changes` lists documents gained and lost (by lineage) and the faithfulness before and after |
| DELETE | `/retrieval/history/{id}` | Your own; a `knowledge.manage` holder may delete anyone's |
| DELETE | `/retrieval/history` | All of your own |

**History is personal.** Others see only their own questions. A `knowledge.manage` holder
can see what people ask a base they can see, but not the passage text, answer or claims
of another person's question: those were retrieved under the asker's document access,
which the manager may not share. A replay always runs under the replayer's own access.

In the console, the **History** tab lists questions with their faithfulness. Click one
to read it, or press **Replay** to see then and now side by side.

---

## Evaluation sets

An evaluation set is a list of test questions for one knowledge base. Each question is
labelled with the documents that answer it. A run asks every question with chosen
settings and scores what came back, so two configurations can be compared on the same
questions.

**Labels are documents, not passages.** "The travel policy answers this" is something a
person can judge. "Chunk 14 answers this" stops being true the next time the document is
re-chunked. Labels are stored as **lineages**, so a question keeps working after a new
version of its document is uploaded. Give any version's id when labelling.

### Metrics

Retrieved passages are reduced to their **distinct documents**, in the order each first
appears. For each labelled question:

| metric | definition |
|---|---|
| hit | at least one expected document was retrieved |
| recall | expected documents retrieved ÷ expected documents |
| precision | retrieved documents that were expected ÷ retrieved documents |
| reciprocal rank | 1 ÷ position of the first expected document (0 if none) |

A run's summary averages them over labelled questions: hit rate, recall, precision and
**MRR**. It also records the measured mean latency per question and the number of
questions that errored. Unlabelled questions are run and shown but left out of the
averages, because a question with no right answer cannot be got wrong. With no labelled
questions the averages are `null`, not zero.

A question that errors (for example, an embedding timeout) is recorded with its error.
The rest of the run still counts.

**Answers (optional).** A run with `answer_model` also writes each question's answer, and
with `judge_model` scores it:

* **faithfulness**: as in the playground, the share of the answer's claims the passages
  support;
* **correctness**, only for questions with an `expected_answer`: the judge compares the
  answer with it, giving agrees (1), partial (0.5) or disagrees (0), with a reason.

The summary adds `faithfulness` and `correctness` (means over the questions judged),
`answered`, `judged`, `answer_errors`, `with_reference`, and `not_answered`. Only the
first 50 questions are answered, because each costs two model calls inside a synchronous
run; the rest are still retrieved and scored. Each question's answer and claims are stored
with the run.

Runs are **synchronous** and capped at 100 questions per set. A run stores its settings,
summary and per-question results (retrieved documents, both scores, ranks and warnings).
So a run stays readable and comparable after the documents or the set change.

### Routes

| method | path | permission |
|---|---|---|
| GET | `/knowledge-bases/{id}/eval-sets` | `knowledge.view` |
| POST | `/knowledge-bases/{id}/eval-sets` | `knowledge.manage`. `name`, `description`, optional `cases` |
| GET / PATCH / DELETE | `/eval-sets/{id}` | view / manage / manage |
| POST | `/eval-sets/{id}/cases` | `knowledge.manage`. `question`, `expected_document_ids`, `expected_answer` |
| DELETE | `/eval-sets/{id}/cases/{case_id}` | `knowledge.manage` |
| POST | `/eval-sets/{id}/runs` | `knowledge.manage`. `top_k`, `candidates`, `score_threshold`, `reranker`, `reranker_model`, `answer_model`, `judge_model` |
| GET | `/eval-sets/{id}/runs` | `knowledge.view` |
| GET | `/eval-runs/{id}` | `knowledge.view`. Includes per-question results |

Scope follows the knowledge base: a set, its questions and its runs are visible to
whoever can see the base, and they answer 404 to anyone who cannot.

Audited: `KNOWLEDGE_BASE_UPDATED` (with each changed setting, from and to),
`DOCUMENT_VERSION_UPLOADED`, `DOCUMENT_REINGESTED`, `EVAL_SET_CREATED`, `EVAL_SET_DELETED`
and `EVAL_RUN_COMPLETED` (with the summary and settings).

In the console, the **Evaluation sets** tab of the Retrieval Playground creates sets,
adds labelled questions, runs them, and lists runs with each metric's change from the
previous run, including faithfulness and correctness when the run was judged. **View**
opens the per-question results; each answer expands to its claims.

---

## Known gaps

* **The judge is not validated.** Faithfulness is one model's reading of another's
  answer. Its claims are shown for checking; no agreement with human ratings is measured.
* **Agent answers are not scored.** Faithfulness is measured in the playground and in
  evaluation runs, not on live agent runs. Agent runs keep their retrieval in the run trace
  (`RAG_SEARCH` events), as before.
* **Runs are synchronous**, hence the 100-question cap. A larger set would need a worker,
  like ingestion.
* A whole base is re-embedded with **Rebuild index…** (see
  [rag.md](rag.md#rebuilding-a-whole-base)), not from the evaluation page. Run the set
  before and after to compare.
* **No per-document access control.** Scope stays per knowledge base.
