# Workflows

Expansion phase E6 (plan §5.10). A workflow is a graph of steps: model calls, knowledge
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

- A definition has exactly one `start` node and at least one `end` node, and no cycles.
- Node ids are lowercase (`[a-z][a-z0-9_]*`). It can have at most 50 nodes.
- `POST /workflows/{id}/validate` lists every problem, each tagged with its node, and
  returns the execution order. The console shows the same list above the graph.

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
- **Expiry (E17):** after `agents.approval_ttl_hours` (default 72, 0 = never) without a
  decision, the approval becomes `EXPIRED` and the run fails. The `rejected` branch is not
  taken. Audited as `WORKFLOW_APPROVAL_EXPIRED` by `scheduler`. See
  [agents.md](agents.md#expiry-e17).

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

## The worker

The workflow worker runs inside the API process, like the ingestion worker, when
`WORKERS__ENABLED` is true. Each tick does one unit of work in its own transaction:

- it queues one scheduled run that is due, or
- it advances one run that is queued or whose delay has elapsed.

Claims use `FOR UPDATE SKIP LOCKED`, so replicas take different rows. A run paused for
approval is continued by the decision itself, not by the worker.

The engine is the platform's own, kept on Postgres. Cron times come from APScheduler's
`CronTrigger`, which is already a dependency. There is no APScheduler job store and no
separate queue. Plan §5.10 asks for a persisted deterministic engine without
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
- **Moving:** `PUT /scope` accepts `resource_type: "workflow"` (needs `workflow.edit`).

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

- **Typed input and output schemas per node.** Outputs are documented per type, and
  validation checks structure but not the paths a template uses.
- **Drag-and-drop canvas.** The editor is a node list, a connection table and a
  JSON view, with an auto-laid-out graph preview. Saved `position` values are kept for a
  future canvas but not used for layout.
- **Parallel execution of independent branches.** Steps run one at a time in topological
  order.
- **Webhook signature verification** (HMAC of the body). The token is a bearer secret.
- **Sub-workflows and loops.**
