# Model governance and deployment revisions

Expansion phase E4 (plan §5.3, §5.4). Every model in the catalogue now carries an
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
- **Upgrading changes nothing.** The migration marks every model that existed before E4
  as `approved`, because it was already in use.
- Approval status is independent of `status` (REGISTERED / AVAILABLE / …). `status`
  says whether the weights are present; approval says whether the organisation allows
  the model.
- **Deprecating a model does not stop anything.** It only refuses new deployments.
- **Blocking a model does not stop its containers either.** The gateway stops routing to
  it straight away, including agent calls, which resolve models the same way.
  - Through an alias with a fallback alias, calls go to the fallback. The fallback must
    not itself be blocked.
  - Called by its own name, or through an alias with no usable fallback, the caller gets
    **403** with code `model_blocked`. The message names what the caller asked for,
    never the model behind an alias.
  - Stop the deployments when convenient. Restarting a blocked model's deployment is
    allowed; changing its configuration is not.
- Re-running manifest import updates license, provenance, family and tags from the
  manifest. It **never** changes the approval status, so a re-import cannot undo a block.

### Who may change it

The new permission **`model.approve`** is held by `AI_ADMIN`, plus `ADMIN` and
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
| `source_reference` | where the weights came from, e.g. `huggingface:Qwen/Qwen3-30B-A3B@abc123`. Provenance only: **never fetched** (Rule 4) |
| `publisher` | who released the model |
| `family` | groups versions and quantised variants of the same model; `version` and `quantization` tell them apart |
| `tags` | up to 32 short labels, trimmed and de-duplicated |

- These fields can be set when a model is registered, in a manifest, or later with
  `PATCH /models/{id}` (needs `model.register`, audited as `MODEL_UPDATED`).
- `PATCH` changes catalogue metadata only. The runtime, paths and GPU needs are not
  editable there, and neither is the approval status.
- `GET /models` accepts `approval_status=` and `family=` filters.

**Deviation from the plan.** The plan sketches a separate `model_versions` table. Here
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
  revision 1 for every deployment that existed before E4.
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
   - `tensor_parallel_size` cannot exceed the GPUs the deployment holds. **The GPU set
     never changes in a revision**: a different GPU count is a new deployment, and a
     rollback to a revision that ran on other GPUs is refused.
2. The old container is stopped and removed. The GPU reservation is **kept**, so
   nothing else can take the devices meanwhile. A failed or stopped deployment had
   already released its GPUs; the same devices are reserved again, or the request is
   refused with 409 if something else holds them now.
3. The deployment returns to `SCHEDULING`, and the worker recreates the container. It
   then goes through the normal health check, exactly like a fresh deploy.

Revisions are refused:

- with 409 while the deployment is between steps. Wait for RUNNING, FAILED or STOPPED;
- with 409 for an external runtime, which the platform does not run;
- for a blocked model.

They are audited as `DEPLOYMENT_REVISED` (with the changed fields) and
`DEPLOYMENT_ROLLED_BACK`.

**This is not zero-downtime.** One deployment is one container. While the replacement
loads, calls through an alias with a fallback go to the fallback (E2); other calls get
503. If the new revision fails its health check, roll back to the previous one.

To roll out a newer engine image to one deployment at a time:

1. Load the image on the nodes.
2. List it in the allowlist:
   ```env
   MODELS__RUNTIME_IMAGE_ALLOWLIST='{"vllm": ["vllm/vllm-openai:v0.12.0"]}'
   ```
3. Create a revision with `image` set to it.
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
- blocked handling in the gateway: 403, hidden from `/v1/models`, alias fallback; and
  deprecated models still serving;
- validation without reservation;
- revise, roll back and their refusals;
- the image allowlist;
- the log stream.

`tests/api/test_route_authorization.py` covers the new routes automatically.
