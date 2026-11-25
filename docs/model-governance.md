# Model governance and deployment revisions

Every model in the catalogue carries an
**approval status**, a license and provenance. Every deployment keeps an append-only
**revision history** that can be rolled back. A deploy can be **validated** before
anything is reserved, and its logs can be **followed live**.

Code:
- `backend/app/services/model_registry.py`: metadata and approval;
- `backend/app/services/deployment.py`: validation, revisions, rollback, log following;
- `backend/app/services/gateway.py`: blocked models in the gateway;
- `backend/app/api/v1/models.py`: routes.

## Approval status

| status | deploy | served by the gateway | listed in `/v1/models` |
|---|---|---|---|
| `approved` | yes | yes | yes |
| `experimental` | yes, the deploy wizard warns | yes | yes |
| `deprecated` | **no new deployments** (409) | yes, existing deployments keep serving | yes |
| `blocked` | **no** (409) | **no**: an alias goes to its fallback, otherwise 403 `model_blocked` | no |

- **New models start `experimental`**, however they arrive: the form, the API, manifest
  import, Ollama import or backend discovery. Approval is a person's decision, so no
  import path grants it.
- **Upgrading changes nothing.** The migration marks every model that existed before approval
  status was introduced as `approved`, because it was already in use.
- Approval status is independent of `status` (REGISTERED / AVAILABLE / …). `status`
  says whether the weights are present; approval says whether the organisation allows
  the model.
- **Deprecating a model does not stop anything.** It only refuses new deployments.
- **Blocking a model stops its running deployments.** A blocked model is never
  served, so its containers would only hold GPUs. Each is stopped as from the
  Deployments page, audited `MODEL_STOPPED` with the message `Model <name> blocked`.
  - To keep them (to inspect one, say), send `"stop_deployments": false`, or untick
    *Stop its running deployments too* in the Edit dialog.
  - A deployment that cannot be stopped (its node is unreachable) does not undo the
    block. It is listed in the response's `stop_failed` and keeps running; the gateway
    refuses it either way.
  - **The deployment worker retries.** About once a minute it stops whatever is
    still live of a blocked model, including a deployment that was still being created
    when the model was blocked. These stops are audited as `deployment-worker`. While
    one cannot be stopped, a WARNING alert *Blocked model still running* stays open; it
    clears once the deployment is stopped, by the worker or by hand.
  - The response lists what was stopped in `stopped_deployments`.
- The gateway stops routing to a blocked model straight away, including agent calls,
  which resolve models the same way.
  - Through an alias with a fallback alias, calls go to the fallback. The fallback must
    not itself be blocked.
  - Called by its own name, or through an alias with no usable fallback, the caller gets
    **403** with code `model_blocked`. The message names what the caller asked for,
    never the model behind an alias.
  - Restarting a blocked model's deployment is allowed; changing its configuration is
    not.
- Re-running manifest import updates license, provenance, family and tags from the
  manifest. It **never** changes the approval status, so a re-import cannot undo a block.

### Who may change it

The permission **`model.approve`** is held by `AI_ADMIN`, plus `ADMIN` and
`SUPER_ADMIN` (they hold every permission). It is deliberately separate from
`model.register`: whoever imports a model should not automatically be the one who signs
it off. A unit test asserts that no other role has it.

```http
POST /api/v1/models/{id}/approval
{"status": "blocked", "note": "Failed red-team review"}
```

The change records who made it and when (`approved_by`, `approved_at`), and the note is
shown next to the status. It is audited as `MODEL_APPROVAL_CHANGED`, with the status
before and after.

## Catalogue metadata

| field | meaning |
|---|---|
| `license` | free text, an SPDX id where there is one (`apache-2.0`, `llama3.1`) |
| `source_reference` | where the weights came from, e.g. `huggingface:Qwen/Qwen3-30B-A3B@abc123`. Provenance only: **never fetched** |
| `publisher` | who released the model |
| `family` | groups versions and quantised variants of the same model; `version` and `quantization` tell them apart |
| `tags` | up to 32 short labels, trimmed and de-duplicated |

- These fields can be set when a model is registered, in a manifest, or later with
  `PATCH /models/{id}` (needs `model.register`, audited as `MODEL_UPDATED`).
- `PATCH` changes catalogue metadata only. The runtime, paths and GPU needs are not
  editable there, and neither is the approval status.
- `GET /models` accepts `approval_status=` and `family=` filters.

**Versions and variants.** There is no separate `model_versions` table. Instead,
each version or quantised variant stays its own model row (it has its own weights, path
and deployments), and the `family` column groups them. That keeps every existing
reference to a model id valid, and adds no join to the gateway's hot path.

## Validating a deploy

```http
POST /api/v1/models/{id}/deploy/validate      # same body as /deploy
```

A dry run of the deploy, needing `model.deploy`. It runs the same checks (approval, weights
present, image configured, node online, GPU placement) but reserves nothing and creates
nothing. It reports **every** problem at once instead of stopping at the first. The
response contains:

- `ok`, `errors` and `warnings`. Warnings cover an experimental model, a missing license,
  and a context length above the catalogued one;
- `placement`: where the scheduler would put the model **now**. The real deploy checks
  again, because another deployment can take the GPUs in between;
- `config`: the settings it would start with, defaults filled in;
- `estimates`: GPU count, memory per GPU and context length, taken from the model's
  catalogue entry and labelled `"model catalogue entry, not measured"`. No figure here is
  measured or invented.

The console's Deploy dialog uses it in two steps. **Validate** shows this review, then
**Confirm deploy** submits. Changing any field sends the dialog back to Validate.

## Deployment revisions

A revision is a snapshot of how a deployment runs: image, tensor parallelism, context
length, GPU memory fraction, node and GPUs. It holds nothing secret; environment values are
never copied in.

- Revision 1 is recorded when the deployment is created. The migration recorded
  revision 1 for every deployment that existed before revisions were introduced.
- The history is append-only. A **rollback** does not rewind. It appends a new revision
  copying the old one's configuration, with `rolled_back_from` set, so the history
  always shows what actually ran when.
- `deployment.current_revision` is the revision now applied.

```http
GET  /api/v1/deployments/{id}/revisions                    # model.view, newest first
POST /api/v1/deployments/{id}/revisions                    # model.deploy → 202
     {"max_model_len": 16384, "change_note": "Halve context to fit"}
POST /api/v1/deployments/{id}/rollback                     # model.deploy → 202
     {"revision": 1}
```

### How a revision is applied

1. Validation:
   - `image` must be the runtime's configured image or one listed in
     `MODELS__RUNTIME_IMAGE_ALLOWLIST`. Nothing outside those lists is ever started.
   - `tensor_parallel_size` cannot exceed the GPUs the deployment holds. **The GPU count
     never changes in a revision**: a different count is a new deployment, and a
     rollback to a revision that ran on a different number of GPUs is refused.
2. A strategy is chosen (see [Without downtime](#without-downtime)).
3. **In place:** the old container is stopped and removed. The GPU reservation is
   **kept**, so nothing else can take the devices meanwhile. A failed or stopped
   deployment had already released its GPUs; the same devices are reserved again, or the
   request is refused with 409 if something else holds them now. The deployment returns
   to `SCHEDULING`, and the worker recreates the container. It then goes through the
   normal health check, exactly like a fresh deploy.
4. **New copy (blue/green):** a second deployment row starts with the new settings on
   free GPUs, marked `replaces_id`. The original keeps serving. See below.

Revisions are refused:

- with 409 while the deployment is between steps. Wait for RUNNING, FAILED or STOPPED;
- with 409 for an external runtime, which the platform does not run;
- for a blocked model.

They are audited as `DEPLOYMENT_REVISED` (with the changed fields) and
`DEPLOYMENT_ROLLED_BACK`.

### Without downtime

A revision's `strategy` decides how the new configuration replaces the running one:

| `strategy` | What happens |
|---|---|
| `auto` (default) | Not serving now (FAILED, STOPPED): in place. Another copy of the model running: in place, while the gateway sends calls to the other copies (E58). The only copy: a new copy on free GPUs takes over once healthy. No free GPUs for it: in place, with downtime |
| `in_place` | Restart on the same GPUs. The model is down while it loads if this is its only copy |
| `blue_green` | A new copy on free GPUs takes over once healthy. 409 if there's no room, or the deployment isn't RUNNING |

The response's `strategy`, `downtime` and `message` say what was chosen, and the console
shows the message, as a warning when it means downtime.

**The handover.** The deployment worker drives the new copy through the usual lifecycle.

* **Healthy:** its container moves into the original row in one transaction. The row
  keeps its id, its revision history and its place in aliases and metrics. Then the old
  container is stopped and its GPUs released, and the copy's row is removed. Both copies
  serve for that moment, so no call finds the model missing.
* **Failed:** the copy is removed, and the original keeps serving its old revision. The
  revision stays in the history, marked *Not applied* with the reason.
* An old container that can't be stopped (node unreachable) stays visible as a FAILED
  deployment that still holds its GPUs. Delete it once the node is back.
* Stopping or deleting the original also removes a copy that was taking over. A second
  revision is refused (409) while a copy is starting.
* Audited as `DEPLOYMENT_HANDOVER`, with the outcome.

A blue/green revision needs free GPUs for one more copy, briefly: the same count the
deployment holds, anywhere in the cluster that fits the model.

### Every copy of a model, one at a time

```http
POST   /api/v1/models/{id}/rollout     # model.deploy -> 202; image, tensor_parallel_size, ...
GET    /api/v1/models/{id}/rollout     # model.view; the running or latest rollout
DELETE /api/v1/models/{id}/rollout     # model.deploy; cancel before the next copy
```

A rollout applies one change to every running copy the platform manages. It revises
each copy with `auto` once the one before it is healthy again, so the others keep
serving, and a single-copy model gets a new copy that takes over.

* Every copy is checked before anything starts. A setting one copy can't take (too much
  tensor parallelism for its GPUs, say) refuses the whole rollout with 422.
* **A copy that fails stops the rollout.** The copies not yet reached keep their old
  configuration. Roll the failed one back from its revisions.
* A copy stopped or deleted before its turn is skipped.
* Cancel stops before the next copy; the copy being revised finishes its own revision.
* Driven by the deployment worker with its state in `deployment_rollouts`, so a
  control-plane restart resumes it. Audited as `DEPLOYMENT_ROLLOUT_STARTED` and
  `DEPLOYMENT_ROLLOUT_FINISHED`.

In the console, the Revisions dialog has a strategy choice and *Every copy, one at a
time*, and shows a running rollout's progress with *Cancel*.

### A canary first

A rollout can watch its first revised copy on a share of real calls before it touches
the rest:

```http
POST /api/v1/models/{id}/rollout
{"image": "vllm/vllm-openai:v0.12.0", "canary_percent": 10, "canary_minutes": 30,
 "canary_max_error_percent": 1}
```

* Once the first copy is revised and healthy, it gets `canary_percent` of the model's
  new calls (1–99) for `canary_minutes` (default 15). The other copies, still on the old
  configuration, take the rest and wait.
* Then its calls in that time are counted from the usage records. If more than
  `canary_max_error_percent` (default 1) failed with a 5xx, the rollout stops (`FAILED`,
  naming the count) and the copy is set to take **no new calls**; roll it back from its
  revisions, or give it its share again. Otherwise it goes back to a full share and the
  other copies follow one at a time. A canary that got no calls passes.
* It needs at least two running copies (422 otherwise), so the old configuration keeps
  serving. Cancelling during the watch gives the copy its full share back.
* The rollout's `canary` shows the settings and what happened: `state` (`watching`,
  `passed`, `failed`, `cancelled`), `deployment_ids`, `until`, `calls`, `errors`, and with
  a latency limit `latency_p95_ms`, `baseline_p95_ms` (or `latency_note`); with a first
  token limit `ttft_p95_ms`, `baseline_ttft_p95_ms` (or `ttft_note`); with a quality limit
  `faithfulness`, `baseline_faithfulness` (or `faithfulness_note`).

**More than one canary copy.** `canary_copies` (default 1) copies are revised first, each
on the canary's share as soon as it is healthy, then watched together; their calls and
errors are added up. A fault that shows only under some load, or on one node, has more
calls to show in. At least one copy keeps the old configuration (422 otherwise), and a
failed watch takes every canary copy off new calls.

**Latency.** With `canary_max_latency_ratio` (above 1, e.g. `1.5`), the canary copies'
p95 latency over the watch is compared with the old copies' over the same minutes, on calls
that didn't fail. More than that many times slower stops the rollout as errors do. With
fewer than 10 such calls on either side latency isn't judged, and `latency_note` says so:
a quiet model can't show a difference.

**Time to first token.** Latency is end to end, so a canary that happens to get longer
prompts or answers looks slower. `canary_max_ttft_ratio` (above 1) compares the p95 time to
first token instead, on streamed calls only (buffered calls have none), with the same
10-call minimum (`ttft_note` otherwise).

**Answer quality.** With [answer scoring](agents.md#answer-quality) on, each judged agent
answer records the copy that wrote it. `canary_max_faithfulness_drop` (0 to 1, e.g. `0.1`)
compares the average faithfulness of answers the canary copies wrote during the watch with
the old copies': more than that much lower stops the rollout. Answers are sampled and
judged in the background, so only those already judged when the watch ends count; with
fewer than 5 on either side quality isn't judged (`faithfulness_note`). A longer watch, or
a higher `agents.answer_scoring_percent` while it runs, gives it more to go on. Only agent
answers that retrieved passages are judged; direct API calls never are.

```http
POST /api/v1/models/{id}/rollout
{"max_model_len": 16384, "canary_percent": 20, "canary_copies": 2,
 "canary_max_latency_ratio": 1.5, "canary_max_ttft_ratio": 2,
 "canary_max_faithfulness_drop": 0.1}
```

In the console: *Canary first* under *Every copy, one at a time*.

### A copy's share of traffic

Each copy has a `traffic_weight`, 0 to 100 (default 100): its share of the model's new
calls relative to the other copies. Copies with equal weights are spread as before (the
least busy first); with different weights, each copy is first for its share of calls. A
copy at 0 takes no new calls while another copy can, but is still tried when the others
fail. Set it by hand for a manual canary (a small share, then more), or to drain a copy
before stopping it:

```http
PUT /api/v1/deployments/{id}/traffic-weight      {"traffic_weight": 10}
```

`model.deploy`, audited as `DEPLOYMENT_TRAFFIC_WEIGHT_CHANGED`. In the console: *This
copy's share of traffic* in the Revisions dialog; the Deployments page marks a copy not at
100.

To roll out a newer engine image:

1. Load the image on the nodes.
2. List it in the allowlist:
   ```env
   MODELS__RUNTIME_IMAGE_ALLOWLIST='{"vllm": ["vllm/vllm-openai:v0.12.0"]}'
   ```
3. Create a revision with `image` set to it, or a rollout for every copy.
4. Roll back if it misbehaves.

## Following logs

```http
GET /api/v1/deployments/{id}/logs/stream?tail=200        # model.view, text/event-stream
```

- `event: lines`, with `data: {"lines": [...]}`, carries the new lines since the last
  event.
- `event: end`, with `data: {"reason": "timeout" | "no_container"}`, is sent last.
- The server polls the container's logs every two seconds and sends only the new
  lines. It ends the stream after ten minutes, and the console offers Reconnect.
- A deployment with no container (external, or long gone) sends its stored failure
  excerpt once.
- The stream holds no database session. The node and container are resolved before the
  response starts.
- The console reads it with `fetch`, not `EventSource`, because `EventSource` cannot
  send the Authorization header.

The existing `GET /deployments/{id}/logs` (one snapshot) is unchanged.

## Console

- **Model Registry:**
  - an approval pill on every row (hover for the note);
  - license, publisher, version and tags under the name;
  - a filter by approval status, and counts per status;
  - grouping by family;
  - an **Edit** dialog for metadata. Holders of `model.approve` also get the approval
    status and note there;
  - Deploy is disabled for blocked and deprecated models.
- **Deploy dialog:** Validate → review (errors, warnings, placement, configuration,
  labelled estimates) → Confirm deploy.
- **Deployments:**
  - the revision number on each row;
  - a **Revisions** dialog with the history, a new-revision form and Roll back;
  - **Logs** now follow live.

## Tests

`tests/api/test_model_governance.py` covers:

- registration defaults and metadata;
- `PATCH`;
- approval permission and audit;
- the filters;
- deploy refusal for blocked and deprecated models;
- blocking stops running deployments, unless asked not to, and reports one it could not
  stop (`tests/api/test_block_stops_deployments.py`);
- the deployment worker's retry and its alert (`tests/api/test_blocked_sweep.py`);
- blocked handling in the gateway: 403, hidden from `/v1/models`, alias fallback; and
  deprecated models still serving;
- validation without reservation;
- revise, roll back and their refusals;
- the image allowlist;
- the log stream.

`tests/api/test_route_authorization.py` covers the new routes automatically.
