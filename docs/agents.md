# Agents

The reference scenario, made real: an administrator assembles an "IT Support Agent" from a
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

## The tool pipeline

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
yourself. This is the single most important property of the agent system, and it has a test named
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

Agents must never have unrestricted shell execution, so these tool types are
**registerable but never executable** — catalogued so an operator can see what
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

Verified across a real control-plane restart by the acceptance gate — a claim no test inside
one process can make.

`POST /runs/{id}/approve` requires **`tool.approve`**, deliberately separate from
`tool.execute`: approving a HIGH-risk action is a different privilege from
performing a routine one. A non-superuser cannot approve a run they started themselves.

An approved call goes **straight to execution**, not back through the pipeline —
re-authorising could reach a different decision than the one the human was shown.

Four approval outcomes, kept distinct: `APPROVED`, `REJECTED` (a human refused),
`EXPIRED` (nobody answered), `NOT_REQUIRED`. Conflating expiry with rejection would
attribute a timeout to a person who never saw it.

### Expiry

An approval nobody answers expires after `agents.approval_ttl_hours` (default 72, a
runtime setting on the Settings page; 0 waits forever). The `approval_expiry` job checks
every 15 minutes.

- The approval becomes `EXPIRED`, never `REJECTED`, and is audited as
  `TOOL_APPROVAL_EXPIRED` or `WORKFLOW_APPROVAL_EXPIRED` by `scheduler`, not by a person.
- The run **fails** with an error saying the approval expired and nobody decided. An agent
  run gets a final `RUN_FAILED` event in its trace.
- The run is not continued as though someone had refused. An agent is not resumed with
  "permission refused", which would spend model time on a refusal nobody made. A workflow
  does not take its `rejected` branch, because that branch was designed for a real
  rejection.
- A decision that arrives after expiry gets the usual 409 ("not waiting for approval").
  Both sides change the run with a conditional update, so a decision at the same moment
  either wins outright or is refused.
- Start the run again if it is still needed.

### Reminders

An approval that will expire within `agents.approval_reminder_hours` (default 24, a
runtime setting; 0 = off) raises a **reminder alert**, checked by the same 15-minute job.
With the defaults, a reminder appears after 48 hours of waiting, 24 hours before expiry.

- One WARNING alert per approval, source `approvals`, titled with the tool and agent (or
  the workflow step) and the time left, for example *Approval for create_ticket (IT
  Helpdesk) expires in 12 hours*. Each check refreshes the time left.
- It shows on the **Alerts** page. A notification channel subscribed to `approvals` (or to
  every source) announces it once, by webhook or email: point one at the approvers' chat.
- It clears at the next check after someone approves or rejects, or after the approval
  expires.
- A reminder window as long as the expiry reminds as soon as an approval is requested.
- **Approvals that never expire.** While `agents.approval_ttl_hours` is 0, set
  `agents.approval_reminder_waiting_hours` (default 0, off) to remind about each approval
  that has waited that long, for example *Approval for create_ticket (IT Helpdesk) has
  waited 3 days*. It is the same alert, cleared the same way, and emailed to approvers
  when that is on (see below). While approvals do expire, this setting is ignored.
- At most 200 reminders per kind are raised per check.

### Reminder emails to each approver

Turn on `agents.approval_reminder_email_approvers` (Settings page, *Security*; off by
default) and each reminder is also emailed to the people who could decide it, at the same
check that raises the alert:

- active accounts holding `tool.approve` (an agent's tool call) or `workflow.approve`
  (a workflow step), and superusers;
- who can see the agent's or workflow's team: its members, holders of `scope.all`, and
  superusers. A shared agent or workflow asks every holder;
- not whoever started the workflow run, unless the step allows self-approval, because they
  could not decide it. A superuser who started it is still asked;
- who have not turned these emails off for themselves.

At most 25 people per approval: explicit holders first, then superusers, by username.
Each person is emailed **once per approval**, however many checks repeat the reminder.
The email goes to the address on the account, through the mail server the notification
channels use (`NOTIFICATIONS__SMTP_*`). Without one, nothing is queued and the worker logs
`approver_reminders_no_mail_server`.

Sends go through an outbox (`approver_reminders`, migration `f7b9d1e3a5c6`) and are
retried with the channels' back-off up to `NOTIFICATIONS__MAX_ATTEMPTS`. One still waiting
when its approval is decided or expires, when the account is turned off, or when the
setting is turned off is cancelled rather than sent. Finished rows are deleted after 30
days. A failed send is logged by account id, never by address.

**Turning them off for yourself.** Click your name at the top of the console. Under
*Email*, switch off *Email me reminders about approvals I can decide*. The switch appears
only if you hold `tool.approve` or `workflow.approve`. You can also call
`PUT /api/v1/users/me/preferences` with `{"approval_reminder_emails": false}`. You are then
left out before the 25-person cap is applied, and any email already queued for you is
cancelled. The change is audited as `USER_UPDATED`. Alerts and channels still announce the
approval.

The channel reminders above are unchanged: point a channel at the approvers' chat as well,
or instead.

## Stopping every agent at once

*Stop all agents* on the Agents page (or the runtime setting `agents.kill_switch`, under
Security on the Settings page) is the switch for an incident: an agent misbehaving, a
tool that turned out to be dangerous, a model giving bad answers. While it is on:

- **No run starts**: from the console, the API (`POST /agents/{id}/execute`), voice, or an
  `agent:<slug>` model through `/v1`, which answers 503 with the code `agents_stopped`
  before any quota is taken.
- **No approval is accepted.** A run waiting for one keeps waiting (it can still expire)
  and can be approved once agents are resumed.
- **Running agents stop before their next model or tool call.** The run ends `FAILED`
  with *Stopped by an administrator* and a `RUN_FAILED` event in its trace. The call in
  progress when the switch was turned on still finishes.

Every refusal is audited as `AGENT_EXECUTED` with result `DENIED`, and turning the switch
on or off is audited as `SETTING_UPDATED` with the reason given. Other replicas follow
within 10 seconds. Everyone who can see the Agents page sees a red banner while agents are
stopped (`GET /agents/status`, which needs only `agent.view`); the button needs
`settings.manage`.

Workflows are not affected, including their model and tool steps. Disable a workflow, or
the tool, to stop those.

## Teams: what an agent may use

An agent runs as **its own team**, so its model and knowledge bases must be ones that
team can see: shared ones, or its team's. A shared agent may use only shared models and
knowledge bases. This is checked when the agent is saved (422), when it is moved with
`PUT /scope` (409), and again **every time it runs** (409, and no run is created). A
model that moved to another team after the agent was saved is therefore refused at the
next run, with a message saying how to fix it. Traces, voice sessions and memory follow
the agent's team too. See [teams.md](teams.md#what-an-agent-or-workflow-may-use).

---

## The runtime, and why it is not LangGraph

`PlatformAgentRuntime` implements the platform's `AgentRuntime` interface directly: a ReAct loop
using `LLMProvider` for inference, the tool pipeline for authorisation, and
`agent_runs.checkpoint` for suspend/resume.

The platform's general principle is to integrate rather than implement, and that is usually right — it is why
vLLM, Qdrant and Open WebUI are here unforked. **This is a documented deviation**, taken
for reasons specific to this platform:

| | LangGraph | Native |
|---|---|---|
| New packages in the air-gapped bundle | 38 | 0 |
| PostgreSQL drivers | 2 (psycopg 3 **and** asyncpg) | 1 |
| Telemetry client to muzzle | `langsmith` | — |
| Adapters needed | 3 (tools, chat model, events) | 0 |
| Run state | LangGraph's tables, opaque blobs | `agent_runs.checkpoint`, queryable |

The second row would reverse the decision to ship one PostgreSQL driver in the bundle. The last row
matters more than it looks: this platform must back that state up, audit it,
and answer "what was the agent about to do?" from the database.

`AgentRuntime` is the seam that makes this reversible — a `LangGraphAgentRuntime`
implementing the same interface is an addition, not a rewrite. Building the interface
first, before any implementation, is what made the choice possible at all.

---

## Skills

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

## MCP servers

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

`mcp/ldap/` answers from a **fixture directory**, so the reference scenario runs without an
Active Directory — the same reasoning as `mock-vllm`. Every response says so. Acting on a
fabricated lockout reason for a real employee is the one harm this could do, so it is made
impossible to mistake.

Replacing it with a real implementation means swapping `_DIRECTORY` and two functions for
`ldap3` calls. The MCP surface and everything the platform stores stay identical — the
point of an MCP server being a separate process behind a protocol.

---

## Agents in chat

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

The end user forwarded by the frontend is recorded as who *asked* but grants
nothing: an Open WebUI account is not a platform account, and treating a forwarded string
as an authorisation subject would make the permission intersection meaningless.

A consequence worth stating plainly: **an agent invoked through chat can only use tools the
API client's owner is permitted to use.** An operator who grants an agent a tool and then
sees it refused in chat is looking at that rule working.

---

## The event model

Persisted to `agent_run_events` **as events happen**, not at the end — a run that fails
half-way must still show what it did, which is the trace an operator needs precisely when
things went wrong.

```
RUN_STARTED · LLM_REQUEST · LLM_RESPONSE · TOOL_REQUESTED
TOOL_APPROVAL_REQUIRED · TOOL_APPROVED · TOOL_REJECTED · TOOL_EXECUTED
RAG_SEARCH · MEMORY_READ · MEMORY_WRITE
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

## Answer quality

A share of agent answers can be judged for faithfulness to the knowledge-base passages
they were given: the same judge as the retrieval playground
([rag-evaluation.md](rag-evaluation.md#the-answer-and-how-faithful-it-is)), on real answers.

| setting | default | |
|---|---|---|
| `agents.answer_scoring_percent` | 0 | share of answers judged, 0–100. 0 judges none |
| `agents.answer_judge_model` | empty | the alias or model that judges. Empty judges none |
| `agents.answer_score_retention_days` | 90 | how long scores are kept |
| `agents.answer_judge_retries` | 2 | judge calls retried after a failure; 0 does not retry |
| `agents.answer_judge_retry_minutes` | 5 | wait before the first retry, doubling after |

- **Which answers.** A run that completed with passages retrieved, sampled at that rate
  when it finishes. A run that paused for an approval counts too: its question and
  passages are kept in its checkpoint (which already holds them as the conversation's
  context) until it resumes and completes. Answers with no passages, and failed runs,
  are not judged.
- **Off the answer's path.** The answer is queued with its question and passages, and a
  worker judges queued answers one at a time, so judging never slows an answer down.
- **A judge that is down is tried again.** A call that fails (the model is down, over
  budget, timed out) leaves the answer queued for `agents.answer_judge_retry_minutes`,
  then twice that, and so on, each with up to a quarter more at random, so the answers
  queued during an outage reach the judge spread out. After
  `agents.answer_judge_retries` retries it is `FAILED` with the last error. A reply that
  came back but couldn't be read fails at once. Each answer shows its `attempts`.
- **Charged like any platform call**: through the gateway, to the agent's team and the
  person who asked, as `agent-judge:<slug>`.
- **The passages are not kept.** They were read under the asker's document access; once
  judged, only the claims (sentences of the answer) and their verdict remain.
- **Where to look.** On the Agents page, *Quality* shows the last 30 days (average
  faithfulness, answers judged, unsupported claims) and each judged answer with its
  claims, unsupported ones in red. `GET /agents/{id}/answer-scores` returns the same, to
  anyone who can read the agent's runs.

- **An alert when it falls.** With `agents.answer_faithfulness_alert_below` set (0 to 1,
  for example 0.7), each judged answer compares the agent's average over its last
  `agents.answer_faithfulness_alert_answers` (default 20) judged answers with that line.
  Below it, a WARNING alert names the agent and its average and goes to its team's
  channels; back at or above it, the alert clears. No alert until the agent has that many
  judged answers.
  An alert no new answer would revisit is cleared by an hourly check: when the agent is
  disabled or deleted, the line is turned off, or the agent has had no judged answer for
  `agents.answer_faithfulness_stale_days` (default 14).

The judge is a model and can be wrong: read the claims, not just the number. Use a capable
model, ideally not the one the agent uses.

## Known gaps

* **Every agent run is accounted.** An `agent:<slug>` call through `/v1`
  writes one `usage_records` row for the whole run against the key, and is admitted
  against its quotas. Runs started from the console, the API
  (`POST /agents/{id}/execute`) or voice, and the part of any run that continues after an
  approval, write an `agent.run` row for the person who started the run and the agent's
  team. Those count toward team and model budgets but are not refused by them. See
  [gateway-policy.md](gateway-policy.md#the-platforms-own-model-calls).
* **Conversation history is not replayed into an agent.** Each run is a fresh
  authorisation with its own trace; feeding it turns it did not run would attribute
  someone else's messages to it. Multi-turn agent conversations need a decision about
  whose authorisation covers the earlier turns.


## Shipped agents

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
using one. `policy-assistant` also runs at temperature 0.1, because it quotes
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

Editing a manifest and importing it again creates a new agent **version**. A run
records the version it executed; rewriting that version in place would make every earlier
run unexplainable — which is the whole reason versions exist.
