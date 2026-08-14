# Agents (M10, M14)

The §20 MVP scenario, made real: an administrator assembles an "IT Support Agent" from a
model, a skill and an MCP server; a user selects it in chat and asks why an employee is
locked out; the agent queries the directory and explains the answer. `make gate-phase4`
runs exactly that.

---

## Agents are versioned, and a run points at a version

`agents` holds identity (slug, display name, enabled). `agent_versions` holds the
executable definition: prompt, model, temperature, iteration cap, and the tool and skill
grants. **A version is never mutated.** An edit publishes the next one and moves
`agents.current_version`.

Without this, an agent edited after an incident makes its own trace unexplainable — the
prompt in the database is no longer the prompt that ran. `agent_runs.agent_version_id` is
`ON DELETE RESTRICT` for the same reason: deleting the version a run executed would erase
the only record of what actually ran.

A run already in flight keeps executing the version it started on, because it holds that
version's id. Editing an agent never disturbs a conversation in progress.

### Partial updates inherit

`PUT /agents/{id}` with only `display_name` keeps the tool grants. The natural bug is to
treat every omitted field as "None means empty" and silently strip an agent's tools on an
unrelated edit; there is a test for it.

---

## The tool pipeline (§10)

Every tool call passes through `app/services/tool_pipeline.py`, in this order:

```
permission check  ->  risk check  ->  approval check  ->  execute  ->  audit
```

No executor is ever called directly by a router or the runtime. An agent that could reach
an executor without passing this function is a privilege-escalation path.

### The permission check is an intersection

A call is allowed only if **the agent version was granted the tool** *and* **the invoking
user holds the tool's `required_permission`**.

Union — "the agent is allowed, so the call is allowed" — is the natural implementation and
it turns every agent into a confused deputy: a way to reach a tool you could not call
yourself. This is the single most important property in the phase, and it has a test named
after it.

The user's permissions are **frozen at run start** in `agent_runs.user_permissions`. A
permission revoked mid-run takes effect on the next run, not half-way through this one; a
permission *granted* mid-run cannot widen a run already in flight.

### A refusal is information, not a crash

The agent is told, in words it can act on — "you do not have permission to use
`ldap_lookup_user`; it requires `directory.read`" — and carries on. It may well answer
another way. Raising would let a prompt-injected instruction end a run.

Denials are audited via `record_independent`, so they survive whatever happens to the
request transaction. An audit trail that loses exactly the refusals reads as "nothing was
ever refused".

### `PYTHON` and `COMMAND`

§M12 lists them; §25 forbids unrestricted shell execution by agents. Reconciled by making
them **registerable but never executable** — catalogued so an operator can see what
exists, refused at execution.

Two independent reasons they cannot run: the pipeline refuses the type, *and*
`build_executors()` contains no entry for them. A future change to one check still cannot
reach a shell.

---

## Approval is a row, not a callback

A HIGH or CRITICAL tool suspends the run at `WAITING_FOR_APPROVAL`. Everything needed to
continue goes into `agent_runs.checkpoint` and `tool_executions`:

```
TOOL_REQUESTED -> TOOL_APPROVAL_REQUIRED   [run suspends, process may exit]
                       ⋮  (days)
TOOL_APPROVED -> TOOL_EXECUTED -> LLM_REQUEST -> LLM_RESPONSE -> RUN_COMPLETED
```

Verified across a real control-plane restart in the Phase 4 gate — a claim no test inside
one process can make.

`POST /runs/{id}/approve` requires **`tool.approve`**, deliberately separate from
`tool.execute` (§10, §M24): approving a HIGH-risk action is a different privilege from
performing a routine one. A non-superuser cannot approve a run they started themselves.

An approved call goes **straight to execution**, not back through the pipeline —
re-authorising could reach a different decision than the one the human was shown.

Four approval outcomes, kept distinct: `APPROVED`, `REJECTED` (a human refused),
`EXPIRED` (nobody answered), `NOT_REQUIRED`. Conflating expiry with rejection would
attribute a timeout to a person who never saw it.

---

## The runtime, and why it is not LangGraph

`PlatformAgentRuntime` implements the §28 `AgentRuntime` interface directly: a ReAct loop
using `LLMProvider` for inference, the §10 pipeline for authorisation, and
`agent_runs.checkpoint` for suspend/resume.

The spec's §2 says integrate rather than implement, and that is usually right — it is why
vLLM, Qdrant and Open WebUI are here unforked. **This is a documented deviation**, taken
for reasons specific to this platform:

| | LangGraph | Native |
|---|---|---|
| New packages in the air-gapped bundle | 38 | 0 |
| PostgreSQL drivers | 2 (psycopg 3 **and** asyncpg) | 1 |
| Telemetry client to muzzle | `langsmith` | — |
| Adapters needed | 3 (tools, chat model, events) | 0 |
| Run state | LangGraph's tables, opaque blobs | `agent_runs.checkpoint`, queryable |

The second row reverses a Phase 0 decision (one driver in the bundle). The last row
matters more than it looks: this platform must back that state up (M25), audit it (M24),
and answer "what was the agent about to do?" from the database.

`AgentRuntime` is the seam that makes this reversible — a `LangGraphAgentRuntime`
implementing the same interface is an addition, not a rewrite. Building the interface in
Phase 0, before any implementation, is what made the choice possible at all.

---

## Skills (M11)

A skill is a reusable, versioned instruction package: name, description, instructions,
`required_tools`, `required_knowledge`, `parameters`, `required_permission`.

A separate entity rather than prompt text on an agent because the reuse *is* the point:
one "diagnose an AD lockout" skill, improved once, applies everywhere.

**Skills append to the agent's prompt, never replace it.** The agent's own instructions
set its character and boundaries; a skill adds a capability within them. The other order
would let a shared skill silently override an agent's constraints.

`required_tools` holds *names*, not ids: a skill ships as a file with the bundle and must
be installable before the tools it wants exist.

---

## MCP servers (M13)

The platform registers servers, health-checks them, discovers their tools, and stores the
metadata. JSON-RPC 2.0 over HTTP, hand-rolled over httpx — the platform uses two methods
(`tools/list`, `tools/call`), and an SDK would add a dependency to the bundle for about
forty lines of JSON.

Health is probed by asking for the tool list. MCP defines no health endpoint, and a server
that answers TCP but cannot list its tools is not usable by an agent.

### Discovery populates; it never grants

A discovered tool arrives **disabled** and marked **HIGH** risk, namespaced as
`<server>.<tool>`. Enabling it, lowering its risk, and assigning it to an agent are three
separate deliberate acts. Otherwise pointing the platform at a server would silently widen
what every agent can do — and nobody has read what a newly discovered tool does.

Re-discovery refreshes descriptions and schemas but **never resets an operator's review**:
`enabled`, `risk_level` and `required_permission` are left alone.

### The shipped LDAP server

`mcp/ldap/` answers from a **fixture directory**, so the §20 scenario runs without an
Active Directory — the same reasoning as `mock-vllm`. Every response says so. Acting on a
fabricated lockout reason for a real employee is the one harm this could do, so it is made
impossible to mistake.

Replacing it with a real implementation means swapping `_DIRECTORY` and two functions for
`ldap3` calls. The MCP surface and everything the platform stores stay identical — the
point of an MCP server being a separate process behind a protocol.

---

## Agents in chat (§M17)

The gateway lists each enabled agent as **`agent:<slug>`** in `GET /v1/models`. A request
naming that prefix runs the agent and returns its answer in chat-completion shape, so Open
WebUI gets agent selection **unforked** — it sees another entry in the picker.

`:` cannot appear in a model name or alias (both are `[A-Za-z0-9._-]+`), so the namespaces
cannot collide.

Tool calls are narrated as visible text (`_Using ldap_lookup_user…_`) when streaming. A
chat user watching an agent sit silent for twenty seconds assumes it has hung, and the
OpenAI chat protocol carries no other progress signal.

### Whose permissions apply

A gateway call carries an API key, not a login. The run is authorised as the **API
client's owner** — the client acts on that person's behalf and must not exceed them. A
client with no owner cannot run agents at all; refusing is the only safe answer, because
the alternative is picking a default identity and silently authorising tool calls as it.

The end user forwarded by the frontend (M17) is recorded as who *asked* but grants
nothing: an Open WebUI account is not a platform account, and treating a forwarded string
as an authorisation subject would make the §10 intersection meaningless.

A consequence worth stating plainly: **an agent invoked through chat can only use tools the
API client's owner is permitted to use.** An operator who grants an agent a tool and then
sees it refused in chat is looking at that rule working.

---

## The event model (§11)

Persisted to `agent_run_events` **as events happen**, not at the end — a run that fails
half-way must still show what it did, which is the trace an operator needs precisely when
things went wrong.

```
RUN_STARTED · LLM_REQUEST · LLM_RESPONSE · TOOL_REQUESTED
TOOL_APPROVAL_REQUIRED · TOOL_APPROVED · TOOL_REJECTED · TOOL_EXECUTED
RAG_SEARCH · MEMORY_READ · MEMORY_WRITE        (Phase 5)
RUN_COMPLETED · RUN_FAILED
```

An explicit `sequence` rather than relying on timestamps, so events order deterministically
when two land in the same millisecond — and so a resumed run continues the sequence rather
than restarting it.

Payloads are redacted: `_credentials_encrypted`, `credentials`, `password` and `token`
never reach a stored event. `agent.view` is a much wider audience than the operators who
may read a tool's credentials.

Tool *arguments* are stored on `tool_executions`, not in the audit log — they can contain
the personal data the tool was called to look up, and `audit.view` is broader still.

---

## Known gaps

* **Agent token usage is not in `usage_records`.** An agent's model calls go through
  `provider_for_model`, which bypasses the gateway's usage recording, so agent traffic is
  counted in `agent_runs.prompt_tokens`/`completion_tokens` but is invisible in
  `GET /api/v1/usage` and the dashboard's gateway panel. Two accounting systems for the
  same GPU time. Deliberately not half-fixed: whether agent tokens should also appear as
  gateway usage (and risk double-counting) is a reporting decision, not an implementation
  detail.
* **No approval expiry worker.** `expire_older_than` exists and is tested, but nothing
  calls it on a schedule yet, so a run nobody answers waits indefinitely.
* **`DATABASE` and `OPENAPI` tools are catalogued but not executable.** They report
  themselves unimplemented rather than pretending.
* **Conversation history is not replayed into an agent.** Each run is a fresh
  authorisation with its own trace; feeding it turns it did not run would attribute
  someone else's messages to it. Multi-turn agent conversations need a decision about
  whose authorisation covers the earlier turns.


## Shipped agents (M10)

Declarative, in `agents/*.yaml`. Import them with:

```bash
make definitions-import          # or POST /api/v1/definitions/import
```

| slug | what it does | tools |
|---|---|---|
| `policy-assistant` | Answers from the organisation's own documents, with citations | none |
| `ops-assistant` | Reports platform health and explains the states | `platform_status`, `current_datetime` |
| `correspondence-assistant` | Drafts and replies in Arabic or English | none |
| `analyst-assistant` | Works through figures and deadlines from source documents | `calculator`, `date_calculator`, `current_datetime` |
| `platform-guide` | Explains what this installation can do, and whether it is running | `model_catalog`, `platform_status`, `current_datetime` |
| `minutes-assistant` | Turns notes into minutes: decisions, owners, absolute dates, and what the notes never said | `date_calculator`, `current_datetime`, `text_statistics` |
| `briefing-assistant` | Writes a short sourced brief, measured against its length limit | `text_statistics`, `calculator`, `date_calculator`, `current_datetime` |

`analyst-assistant` pairs the quantitative and document skills deliberately. An analyst
agent with only one of the two produces the most dangerous output there is — a
well-cited report whose numbers do not add up, or correct arithmetic on figures nobody
can trace.

`minutes-assistant` and `briefing-assistant` pair their content skill with
`sensitive-information-handling` for the same kind of reason. Minutes and briefs both
circulate more widely than the material behind them, so the document boundary is where
personal circumstance leaks — and an agent good at extracting commitments is, by
construction, good at extracting everything else in the record too.

`briefing-assistant` runs at 10 iterations because cutting to a length is iterative. It
measures the draft with `text_statistics` and cuts until it fits, rather than asserting a
word count: a limit checked once is a limit usually missed, and a model cannot count its
own output.

`platform-guide` exists because "what can this thing do" is the first question every new
user asks and the one an assistant is worst at: it answers from what systems like it
usually do, which on an air-gapped site running a specific bundle is a guess.

Two of the seven have **no tools at all**, and that is the design rather than an
omission. Retrieval happens before the model is called, so a document-answering agent
needs no capability to reach anything — and an agent with no tools cannot be talked into
using one (§10). `policy-assistant` also runs at temperature 0.1, because it quotes
documents and invention is its failure mode; `correspondence-assistant` runs at 0.4,
because it writes prose and 0.1 produces stilted drafting.

`policy-assistant` ships with no knowledge bases attached — base names are site-specific.
Add one in the admin console, or name it under `knowledge_bases:` and re-import.

### Import order, and why it matters

Tools, then skills, then agents. A skill names the tools it requires and an agent names
both, so a later stage resolves what an earlier one created. Importing the other way
round produces an agent with an empty tool list: it exists, it answers, and it can do
nothing — which reads as a model problem rather than an import one.

A name that cannot be resolved does **not** fail the import. The commonest case is a tool
discovered from an MCP server this site has not started, and an otherwise-correct agent
is still worth having. It is reported per file instead, because an agent granted three
tools and given one is not the agent the manifest describes.

### Re-import publishes a version, never an edit

Editing a manifest and importing it again creates a new agent **version** (§M14). A run
records the version it executed; rewriting that version in place would make every earlier
run unexplainable — which is the whole reason versions exist.
