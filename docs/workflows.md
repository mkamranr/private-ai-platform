# Workflows

A workflow is a graph of steps: model calls, knowledge
retrieval, tools, HTTP requests, conditions, transforms, human approvals and delays. It
runs on demand, through the API, on a cron schedule or from a webhook. Every run keeps a
step-by-step trace.

Code:

- `backend/app/services/workflow_definition.py`: the definition format, validation and
  templates (pure functions).
- `backend/app/services/workflow_engine.py`: runs one workflow run.
- `backend/app/services/workflows.py`: lifecycle, triggers and approvals.
- `backend/app/workers/workflows.py`: schedules and resuming paused runs.
- `backend/app/api/v1/workflows.py`: the API.
- Console: the **Workflows** page, plus workflow steps on the **Approvals** page.

## The canvas

The *Builder* tab is a canvas:

- **Add a step** with the buttons above it (Model, Retrieval, Tool, …). The form opens;
  the new step is placed to the right of the last one and connected after it.
- **Move a step** by dragging it. Positions snap to an 8-pixel grid and are saved with the
  draft (each node's `position`, in pixels). *Auto-arrange* lays the steps out in columns
  by order again.
- **Connect two steps** by dragging from a dot on a step's right edge to the other step. A
  condition has a `true` and a `false` dot, an approval `approved` and `rejected`; every
  step but start has a red dot underneath for its `error` branch.
- **Edit a step** by clicking it (or Tab to it and press Enter). The form lists the values
  of the steps before it; click one to insert its template where the cursor is.
- **Remove** a step or connection: click it, then *Remove* or <kbd>Delete</kbd>. The start
  step can't be removed.
- **Select several steps** with Shift- or Ctrl-click, or by dragging a box around them on
  empty canvas (Shift keeps the steps already selected); <kbd>Ctrl</kbd>+<kbd>A</kbd>
  selects every step. Dragging one of them moves them all; *Remove* or
  <kbd>Delete</kbd> removes them all (except start).
- **Copy and paste.** <kbd>Ctrl</kbd>+<kbd>C</kbd> copies the selected steps and the
  connections between them, <kbd>Ctrl</kbd>+<kbd>X</kbd> cuts them,
  <kbd>Ctrl</kbd>+<kbd>V</kbd> pastes them a little lower and to the right, and
  <kbd>Ctrl</kbd>+<kbd>D</kbd> duplicates them. Pasted steps get new ids
  (`summarise` becomes `summarise_2`), and their templates that read another pasted step
  (`{{ nodes.search.context }}`) read its copy. What was copied is kept in the browser,
  so it can be pasted into another workflow, in another tab, or after a reload. A pasted
  step isn't connected to the rest: connect it yourself.
- **Undo and redo** every change since the draft was loaded:
  <kbd>Ctrl</kbd>+<kbd>Z</kbd>, <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>Z</kbd> (or
  <kbd>Ctrl</kbd>+<kbd>Y</kbd>), or the arrows above the canvas; up to 100 changes. A
  drag is one change. *Discard changes* still goes back to the saved draft.
- **A reload loses nothing.** Unsaved changes, with their undo history, are kept in the
  browser tab, and the workflow that was open opens again with them (*unsaved changes*).
  They are dropped when saved or discarded, or when the draft was saved meanwhile
  elsewhere (they began from a draft that is no longer the server's). Closing the tab
  drops them.
- **Zoom** with the buttons above the canvas (*Fit* fits its width), <kbd>Ctrl</kbd>+wheel,
  or <kbd>Ctrl</kbd>+<kbd>+</kbd> / <kbd>-</kbd> / <kbd>0</kbd>, from 40% to 200%.
- **Pan** by dragging with <kbd>Space</kbd> held or with the middle button (when only
  viewing a workflow, by dragging empty canvas).
- **Loop back** by connecting a step to one it already follows from: the connection
  becomes an edge back (&#8634;, drawn dashed under the steps), and *Connections* sets how many
  times it may be followed. See [Edges back](#edges-back).
- **Types.** The values a step can insert show their type (`text`, `number`,
  `list of object`...).

The shortcuts work wherever the focus is on the Builder tab, except in a form field or an
open dialog (<kbd>Ctrl</kbd>+<kbd>C</kbd> copies page text instead when some is selected).

The step and connection tables are still under *Steps and connections as a list*, and the
JSON under *Definition (JSON)*. Drafts saved before the canvas stored grid columns rather
than pixels; they are laid out automatically until a step is moved.

## Lifecycle

| status | runs | editable |
|---|---|---|
| `DRAFT` | test runs only | yes |
| `PUBLISHED` | yes | yes (the draft; runs use the published version) |
| `PAUSED` | test runs only; the schedule does not fire | yes |
| `ARCHIVED` | none | no, and it cannot be unarchived |

- **The draft** saves in any state, so you can build a workflow step by step.
- **Publishing** validates the draft and freezes it as the next version (`v1`, `v2`, …),
  with an optional note. A run uses the version it started with, even if a newer one is
  published while it is paused.
- **Versions** are kept. The console's *Copy to draft* restores an old version as the
  draft; publish it to make it current.
- **Deleting** removes the workflow, its versions and its run history. It is refused
  while any run is still in progress. Archive the workflow instead to keep the history.

## The definition

```json
{
  "nodes": [
    {"id": "start", "type": "start", "name": "Question",
     "config": {"required_inputs": ["question"]}},
    {"id": "search", "type": "retrieval", "name": "Search",
     "config": {"knowledge_base_ids": ["<uuid>"], "query": "{{ input.question }}", "top_k": 4}},
    {"id": "answer", "type": "llm", "name": "Answer",
     "config": {"model": "assistant", "prompt": "Context:\n{{ nodes.search.context }}\n\nQ: {{ input.question }}"},
     "retry": {"max_attempts": 2, "backoff_seconds": 1}, "timeout_seconds": 60},
    {"id": "end", "type": "end", "config": {"output": {"answer": "{{ nodes.answer.text }}"}}}
  ],
  "edges": [
    {"from": "start", "to": "search"},
    {"from": "search", "to": "answer"},
    {"from": "answer", "to": "end"}
  ]
}
```

- A definition has exactly one `start` node and at least one `end` node, and no cycles
  except through [edges back](#edges-back).
- Node ids are lowercase (`[a-z][a-z0-9_]*`). It can have at most 50 nodes.
- `POST /workflows/{id}/validate` lists every problem, each tagged with its node, and
  returns the execution order. The console shows the same list above the graph.

### Node schemas

Each node type declares its settings and outputs, and `GET /workflows/node-types` serves
them: for each type its `fields` (`key`, `kind`, `label`, `required`, `templated`, and
`minimum`, `maximum`, `options`, `help`, `placeholder` where they apply), its `outputs`,
its `output_types` (each output's type: `text`, `number`, `boolean`, `object`,
`list of object`, or `any` when only the run knows), its `branches` and whether it is
`timed` (takes retries and a timeout). The console builds
its forms from them. Validation follows the same schema:

- **A setting the type doesn't have** is a problem, naming the ones it does have, so a
  misspelt `temprature` doesn't silently do nothing.
- **Types and ranges**: numbers are numbers (`temperature` 0–2, `top_k` 1–50, `seconds` up
  to a week), lists are lists, choices are one of their options. A value that is only a
  template (`"{{ input.t }}"`) stands in for a typed value where `templated` is true.
- **A template's type against its setting**: a whole template filling a typed setting is
  checked against what it reads, so `temperature: "{{ nodes.search.passages }}"` is a
  problem (*temperature takes a number, but ... is a list of ...*). Text is allowed into
  a number or yes/no setting, and what only the run knows (the input, a transform's
  values) passes. When the run renders it, text that plainly means the value is
  converted (`"0.7"`, `"300"`, `"true"`), and anything else fails the step naming the
  setting (*temperature is a number, not 'warm'*), before any call is made.
- **Templates that can never resolve**: one that reads a step that doesn't exist, a step
  that runs after this one, an output that step doesn't have
  (`{{ nodes.search.contxt }}`: a retrieval step has `passages`, `count`, `context`,
  `warnings`), a `run` field other than `id`, `workflow_id`, `trigger` and `version`, or
  something other than `input`, `nodes`, `previous` or `run`. Any step's `error` can be
  read, and the start step's and a transform's outputs are whatever the input or the
  transform's keys are.
- **A model's JSON shape.** A model step with `parse_json` can declare `json_schema`, a
  JSON Schema of the reply (`type`, `properties`, `items`). Paths under its `json` are then
  checked like any typed output: `{{ nodes.calc.json.totl }}` is a problem when the
  schema's object has `total`. The schema only types the paths; the reply isn't checked
  against it.
- **Deeper paths follow the output's type.** A list is read by index
  (`nodes.search.passages.0.text`; `passages.first` is a problem), an object by its fields
  (a passage has `text`, `document`, `knowledge_base`, `location`, `score`,
  `rerank_score`; a model step's `usage` has `prompt_tokens`, `completion_tokens`,
  `total_tokens`), and text, numbers and booleans have nothing under them
  (`nodes.search.context.size` is a problem). Values only the run knows (a model's `json`,
  an HTTP `body`, a tool's `data`, a sub-workflow's `output`) aren't checked below that.

Already published versions keep running as they are; the stricter checks apply when a
draft is checked or published.

### Node types

| type | config | output |
|---|---|---|
| `start` | `required_inputs`: keys a run must carry | the run input |
| `llm` | `model` (alias or model name), `prompt`, `system`, `temperature`, `max_tokens`, `parse_json` | `text`, `model`, `usage`, and `json` with `parse_json` |
| `retrieval` | `knowledge_base_ids`, `query`, `top_k` | `passages`, `count`, `context` (ready for a prompt), `warnings` |
| `tool` | `tool_id`, `arguments` | `content`, `data`, `truncated` |
| `http` | `method`, `url`, `headers`, `body` | `status`, `body` (parsed JSON or text), `truncated` |
| `condition` | `left`, `operator`, `right` | `result`; branches `true` / `false` |
| `transform` | `output`: an object built from earlier values | that object |
| `approval` | `message`, `details`, `allow_self_approval` | branches `approved` / `rejected` |
| `delay` | `seconds` (up to a week) | `waited_seconds` |
| `workflow` | `workflow_id`, `input` | `output` (the sub-workflow run's output), `run_id`. See [Sub-workflows and loops](#sub-workflows-and-loops) |
| `foreach` | `items`, `workflow_id`, `input` (may use `{{ item }}`, `{{ index }}`), `max_items`, `concurrency`, `on_error` | `results` (each run's output, in order), `count`, `run_ids`, `failed`, `errors` |
| `end` | `output` (empty: the last step's output) | becomes the run's output |

Condition operators are `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `not_contains`,
`empty` and `not_empty`. Numbers compare as numbers, even when one side is text such as
`"10"`.

### Templates

Any string in `config` may refer to earlier values:

- `{{ input.amount }}`: the run input.
- `{{ nodes.search.context }}`: a field of an earlier node's output. Paths go into
  objects and list indexes, e.g. `nodes.search.passages.0.text`.
- `{{ run.id }}`, `{{ run.workflow_id }}`, `{{ run.trigger }}`.
- `{{ previous.draft.text }}`: inside a loop, a step's output on the pass before (empty on
  the first). See [Edges back](#edges-back).

A value that is *only* a template keeps its type. `"{{ input.tags }}"` stays a list, and
`"{{ nodes.calc.json.total }}"` stays a number. Inside longer text a value is converted
to text, and objects become JSON. A missing path renders as empty.

### Edges and branches

- An edge with no `when` follows a successful step.
- After a `condition`, use `when: "true"` or `when: "false"`. After an `approval`, use
  `"approved"` or `"rejected"`.
- **`when: "error"`** is allowed after any step except `start`. It runs when that step
  fails (after its retries) and handles the failure instead of failing the run. The
  failed step's output is `{"error": "..."}`.
- A step runs when any one of its incoming edges is active. A step none of whose edges
  are active is skipped. That is how only one branch of a condition runs, and how two
  branches can meet at a shared step.

### Edges back

An edge with `"back": true` goes to a step that runs before its source, and runs the
steps from that one to the source again: a draft that a check sends back for another try,
or a call repeated until its answer is complete.

```json
{"from": "check", "to": "draft", "when": "false", "back": true, "max_loops": 3}
```

- **Bounded.** It is followed at most `max_loops` times (1–20, default 3) in a run. After
  that, a branch with no other way on fails the run (*went back to 'draft' 3 times*); a
  branch that also goes on carries on.
- **What runs again** is every step on a path from the target to the source, each a new
  step in the run's trace, plus steps after the target that were skipped (the other
  branch of a condition on the way). Steps on other branches don't run again.
- **The pass before** is `{{ previous.<step>.<output> }}`: in the example, the draft can
  read `{{ previous.check.left }}` or its own `{{ previous.draft.text }}`.
- **Rules.** It must go back (to a step its source follows from), not to start or end; it
  follows a branch the source has (not `error`), and that branch can't also go on
  elsewhere; one per branch of a step. Loops inside a loop count again on each outer pass.
- A run is still limited to 200 steps in all.

### Parallel branches

Steps that don't depend on each other run at the same time. Two connections out of one
step without a `when` start two branches, and both run; a step they both lead to waits for
both:

```json
"edges": [
  {"from": "start", "to": "crm"}, {"from": "start", "to": "erp"},
  {"from": "crm", "to": "merge"}, {"from": "erp", "to": "merge"}
]
```

- **A step starts once the steps leading into it are done**, and any earlier step its
  templates read. It doesn't wait for unrelated steps.
- **Model and HTTP calls overlap.** The rest of a step's work (database lookups, recording
  the step, retrieval and tool calls) takes turns, as the run has one database session.
- **A pause on one branch doesn't stop the others.** An approval or delay waits while the
  other branches carry on; the run pauses once nothing else can run. Each delay keeps its
  own due time.
- **An end step runs once every other step is done or skipped**, so a branch that goes
  straight to an end no longer cuts its siblings short.
- A failing step with no `error` branch fails the run once the steps running alongside it
  have finished.

Chains and condition branches run exactly as before: there is only ever one step to run.

### Retries and timeouts

`llm`, `retrieval`, `tool` and `http` nodes take:

- `retry.max_attempts` (1–5).
- `retry.backoff_seconds` (0–60). The wait is multiplied by the attempt number.
- `timeout_seconds` (1–600, default 60) for each attempt.

The step trace records how many attempts were made.

## Runs

```http
POST /api/v1/workflows/{id}/runs
{"input": {"question": "How do I reset my password?"}, "wait": true}
```

- **With `wait`** (the default), the run executes in the request. The response shows
  where it ended: `SUCCEEDED`, `FAILED`, `WAITING_FOR_APPROVAL` or `WAITING` (a delay).
- **With `"wait": false`**, the run is queued, and the worker starts it within seconds.
- **`"test": true`** runs the saved draft instead of the published version. It needs
  `workflow.edit`.

Run statuses: `QUEUED`, `RUNNING`, `WAITING`, `WAITING_FOR_APPROVAL`, `SUCCEEDED`,
`FAILED`, `CANCELLED`.

- `GET /workflow-runs/{id}` returns the run with its frozen definition, every step and
  its approvals.
- Each step records:
  - its rendered input (secret-looking HTTP headers are redacted);
  - its output, or its error;
  - the branch it took, its attempts and its duration.
- The console colours the graph by step status and lists the steps with their inputs and
  outputs.
- `POST /workflow-runs/{id}/cancel` stops a queued, paused or waiting run. Its pending
  approvals expire.
- A run stops after 200 steps.

### Sub-workflows and loops

A **Sub-workflow** step (`workflow`) runs another workflow, and a **For each** step
(`foreach`) runs one once for each item of a list:

```json
{"id": "each", "type": "foreach",
 "config": {"items": "{{ nodes.search.passages }}", "workflow_id": "<uuid>",
            "input": {"text": "{{ item.text }}", "position": "{{ index }}"}, "max_items": 20,
            "concurrency": 4, "on_error": "continue"}}
```

- **A run of its own.** The sub-workflow's published version runs as a separate run, with
  trigger `workflow`, `parent_run_id` and `parent_node_id` set, and the parent's starter
  and permissions. Its steps are in its own trace; the parent's step links to it in the
  console, and the child links back.
- **Items at once.** `concurrency` items run at the same time (1, one after another, when
  empty; up to 10). Their runs advance side by side, so their model and HTTP calls
  overlap. When one finishes, the next item starts in its place, even while others wait
  for an approval. `results` keep the order of `items` whatever order the runs finish in.
  `{{ item }}` and `{{ index }}` are available only in
  the foreach step's `input`. Without an `input`, each run gets `{"item": <the item>}`.
  `items` must render to a list of at most `max_items` (default 20, up to 100), or the
  step fails.
- **A child that pauses** (an approval, a delay) leaves the step waiting, and the parent
  run is `WAITING`. When the child finishes, the parent continues; a foreach then starts
  its next item.
- **A child that fails** (or is cancelled) fails the step, naming the run, and the step
  follows its `error` branch if it has one. A foreach (`on_error` `stop`, the default)
  stops at the first failed item and cancels its items still running. With `on_error`
  `continue` it runs every item and succeeds: a failed item's place in `results` is
  `null`, `failed` counts them, and `errors` lists each one's `index`, `run_id`, `status`
  and `error`. A condition on `{{ nodes.each.failed }}` can branch on them.
- **Cancelling a parent** cancels its unfinished children (and their pending approvals).
- **Not retried or timed out as a whole**: a retry would start the children again. The
  children's own steps have their own retries and timeouts.
- **Bounded.** Sub-workflows nest at most 3 deep below the run a trigger started; a
  deeper one fails the step. That also stops a workflow that ends up calling itself
  through others. One that calls itself directly can't be published.
- **Checked when saved and published.** The target must be a workflow you can see and that
  the workflow's own team may use (shared, or the same team). Publishing, or a test run,
  also needs it to be published and not this workflow itself. A sub-workflow archived
  later fails the step that runs it.

### Whose permissions a run uses

A run captures the permissions of whoever started it when it starts. A `tool` node whose
tool requires a permission fails if the starter lacked it. Scheduled and webhook runs have
no person behind them, so they use the permissions of whoever published the running
version. If that account has been disabled, they run with none.

## Approvals

An `approval` node pauses the run and creates a pending approval. A `tool` node whose tool
is `HIGH` or `CRITICAL` risk does the same before calling it. The risk levels come from
`AGENTS__APPROVAL_REQUIRED_RISK_LEVELS`, the same setting agents use.

- `GET /workflow-approvals/pending` lists pending approvals for the workflows in your
  scope. The console's **Approvals** page shows them under the agent tool calls.
- `POST /workflow-approvals/{id}/decide` takes `{"approved": true|false, "reason": "..."}`.
  It records the decision and continues the run in the same request.
  - An `approval` node takes its `approved` or `rejected` branch.
  - An approved tool is called.
  - A rejected tool follows the node's `error` edge, or fails the run.
- **Self-approval:** whoever started a run cannot decide its approvals (403), unless the
  approval node sets `allow_self_approval`. Approvals for a tool node never allow it. A
  superuser can always decide.
- Every decision is audited as `WORKFLOW_APPROVAL_GRANTED` or `WORKFLOW_APPROVAL_DENIED`,
  with the reason.
- **Expiry:** after `agents.approval_ttl_hours` (default 72, 0 = never) without a
  decision, the approval becomes `EXPIRED` and the run fails. The `rejected` branch is not
  taken. Audited as `WORKFLOW_APPROVAL_EXPIRED` by `scheduler`. See
  [agents.md](agents.md#expiry).
- **Reminders:** `agents.approval_reminder_hours` (default 24) before expiry, the
  step raises a reminder alert (source `approvals`) quoting its message, for the Alerts
  page and subscribed notification channels. See [agents.md](agents.md#reminders).
  With `agents.approval_reminder_email_approvers` on, it is also emailed to each person
  who could decide it, never to the run's starter unless the step allows self-approval
  (see [reminder emails](agents.md#reminder-emails-to-each-approver)).

## Triggers

| trigger | how |
|---|---|
| manual / API | `POST /workflows/{id}/runs` (`trigger`: `manual` or `api`) |
| test | the same with `"test": true`, running the draft |
| schedule | `PATCH /workflows/{id}` with `schedule_cron` (five-field cron, UTC) and `schedule_input` |
| webhook | `POST /workflows/{id}/webhook` returns a token once; callers `POST /api/v1/workflow-hooks/{id}` |

### Schedules

- Only a `PUBLISHED` workflow fires. `next_run_at` is cleared while the workflow is paused
  or archived, and computed again when it is resumed.
- After downtime, a missed schedule fires **once** when the platform returns, not once for
  every slot it missed.
- An empty `schedule_cron` removes the schedule.

### Webhooks

```bash
curl -X POST https://ai.example.internal/api/v1/workflow-hooks/<workflow-id> \
  -H "X-Workflow-Token: wfh_..." -H "Content-Type: application/json" \
  -d '{"order": "SO-88140", "amount": 1450}'
```

- The JSON body becomes the run input. The answer is **202** with the queued run, which
  the worker picks up.
- The token is stored hashed and shown only when it is issued. Rotating it invalidates
  the old one at once, and `DELETE /workflows/{id}/webhook` disables the webhook.
- A missing workflow, a workflow with no webhook and a wrong token all get the same 401,
  so the endpoint does not reveal which workflows exist.

#### Signed calls

The token says who may call; it does not prove the body arrived unchanged, and a captured
call could be sent again. Enabling or rotating a webhook also issues a **signing secret**
(`signing_secret`, shown once, stored encrypted). A signed call adds two headers:

```bash
BODY='{"order": "SO-88140", "amount": 1450}'; TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
curl -X POST https://ai.example.internal/api/v1/workflow-hooks/<workflow-id> \
  -H "X-Workflow-Token: wfh_..." \
  -H "X-Workflow-Timestamp: $TS" -H "X-Workflow-Signature: sha256=$SIG" \
  -H "Content-Type: application/json" -d "$BODY"
```

- The signature is HMAC-SHA256 of `<timestamp>.<raw body>`, the scheme Stripe and GitHub
  use. Several comma-separated `sha256=` values may be sent; one matching is enough.
- A timestamp more than **5 minutes** from the platform's clock is refused, and a
  signature already accepted is refused for 10 minutes after (remembered in Valkey; while
  Valkey is down only the timestamp limits a replay).
- `POST /workflows/{id}/webhook` with `{"require_signature": true}` makes the workflow
  refuse unsigned calls; omit it to keep the current setting. Without it, unsigned calls
  still work, but a signature that is sent must be valid.
- Every refusal is the same 401 as a wrong token; the reason is logged
  (`workflow_webhook_signature_refused`), not returned.
- Rotating replaces token and secret together; disabling the webhook deletes both.
- **Rotating without dropped calls.** Send `{"overlap_minutes": N}` (up to 10080,
  7 days; the console offers 1 hour, 24 hours and 7 days) to keep the replaced token and
  secret working for N minutes while callers switch. Each token is checked with its own
  secret: the old token signed with the new secret is refused. The response's
  `previous_valid_until`, and the workflow's `webhook_previous_valid_until`, say when the
  old pair stops. Rotating again keeps only the pair just replaced, and disabling the
  webhook ends the overlap at once. The default, 0, stops the old pair immediately.
- Upgrade: webhooks enabled before signing was introduced have no secret until rotated.

## The worker

The workflow worker runs inside the API process, like the ingestion worker, when
`WORKERS__ENABLED` is true. Each tick does one unit of work in its own transaction:

- it queues one scheduled run that is due, or
- it advances one run that is queued or whose delay has elapsed.

Claims use `FOR UPDATE SKIP LOCKED`, so replicas take different rows. A run paused for
approval is continued by the decision itself, not by the worker.

The engine is the platform's own, kept on Postgres. Cron times come from APScheduler's
`CronTrigger`, which is already a dependency. There is no APScheduler job store and no
separate queue. The aim is a persisted, deterministic engine without
distributed-workflow complexity, and a run's state lives on its row (`workflow_runs.state`).

## Scope and permissions

| permission | grants | roles |
|---|---|---|
| `workflow.view` | list and read workflows, versions and runs | VIEWER and up |
| `workflow.edit` | create, edit, publish, pause, archive, delete, triggers, test runs | AGENT_ADMIN |
| `workflow.execute` | start and cancel runs of a published version | DEVELOPER, USER, AGENT_ADMIN |
| `workflow.approve` | see and decide pending workflow approvals | AGENT_ADMIN |

Workflows take part in team scope ([teams.md](teams.md)):

- **Ownership:** a workflow has an optional `team_id`. Its runs and approvals belong to its
  team. Out-of-scope workflows, runs and approvals answer 404 and are left out of lists.
- **References:** saving or publishing a definition checks the knowledge bases and model
  names it refers to against your scope. A workflow cannot reach another team's knowledge
  base.
- **Its own team:** the same references must also be usable by the workflow's own
  team, so a shared workflow may use only shared models and knowledge bases. LLM and
  retrieval steps check again when they run, on the rendered values. A step that names a
  model moved to another team since the workflow was published fails with the reason.
  See [teams.md](teams.md#what-an-agent-or-workflow-may-use).
- **Moving:** `PUT /scope` accepts `resource_type: "workflow"` (needs `workflow.edit`). A
  move to a team that cannot use what the workflow refers to is refused with 409.

## Security notes

- **HTTP node URLs** pass the same outbound policy as backends (`core/url_policy.py`):
  - no link-local or cloud metadata addresses, and no public AI services;
  - no credentials in the URL;
  - redirects are not followed, because a redirect could leave the checked host.

  Literal URLs are checked when the draft is validated, and templated ones at run time.
- **HTTP node headers are stored in the definition.** Anyone with `workflow.view` can read
  them. They are redacted in step traces, but not in the definition. For an authenticated
  service, register a REST tool with stored credentials (encrypted, never returned) and
  use a `tool` node.
- Run input is capped at 64 KiB, and HTTP response bodies are truncated at 16 KiB in step
  output.
- Audited: `WORKFLOW_CREATED`, `WORKFLOW_UPDATED`, `WORKFLOW_PUBLISHED`,
  `WORKFLOW_STATUS_CHANGED`, `WORKFLOW_DELETED`, `WORKFLOW_WEBHOOK_ROTATED`,
  `WORKFLOW_RUN_STARTED`, `WORKFLOW_RUN_CANCELLED`, `WORKFLOW_APPROVAL_GRANTED` and
  `WORKFLOW_APPROVAL_DENIED`.

## Not included

- **Types below "any".** An HTTP `body`, a tool's `data`, a sub-workflow's `output` and
  a model's `json` without a `json_schema` are only known when the run has them, so paths
  into them (`nodes.call.body.items`) aren't checked. A template inside other text isn't
  typed: it is always text.
- **Parallel retrieval and tool steps.** They take turns with the run's other database
  work, so only model and HTTP calls truly overlap.
- **A loop's state across versions.** A run keeps its own version's definition, so an
  edge back edited while it runs doesn't change it. Items of a foreach running at once
  share one database session, so only their model and HTTP calls overlap (as for parallel
  branches).
