# Changelog

Each release lists what changed for someone running the platform. Upgrade steps are in
[docs/upgrade-0.2.md](docs/upgrade-0.2.md), and the release procedure is in
[docs/release-checklist.md](docs/release-checklist.md).

## Unreleased

### Restore brings back documents and search too
- **A complete restore.** Restoring a backup now also brings back the stored original documents and the vector collections, not only the database. Search works straight after a restore, with no re-embedding.
- **Or just some of it.** The restore dialog has a checkbox for *Database*, *Stored files* and *Vectors*; `scripts/backup.py restore` takes `--parts`. A backup taken under another encryption key can still restore its files and vectors.
- The stored files a restore replaces are kept beside it (`data/minio.before-restore-<time>`) until you delete them.
- **Upgrade.** Nothing to migrate. Update the ops runner on the host with the rest of the release; an older runner keeps restoring the database only.

### Rebuild a knowledge base with another model or chunk size
- **No more new base and re-upload.** *Rebuild index…* on a knowledge base re-embeds every document with another embedding model, chunk size or overlap. A model change reuses the text already stored; a chunking change parses the stored originals again.
- **No downtime.** The new index is built beside the current one, and searches keep using the current one until the new one is complete. Then the base switches over in one step. A progress banner shows how far it has got, and *Cancel rebuild* throws away what was built.
- Uploads made during a rebuild wait and are indexed once, with the new settings. A rebuild that runs out of token budget pauses until the budget resets. Starting and finishing are recorded in the audit log.
- **Upgrade.** Migration `d4f6b8a0c2e3` adds two tables. Nothing changes until you start a rebuild.

### Several copies of a model now share the work
- **More copies, more capacity.** When a model runs as several deployments, calls are now spread across them: each goes to the copy with the fewest calls in progress. Before, the first copy took every call and the others only stepped in when it failed.
- **A failed call tries another copy.** A call that fails is retried once on another copy before the alias's fallback. A streamed answer moves only if it failed before anything was sent.
- **Your choice.** Settings page, *Gateway resilience*: *Load balancing across copies* (`least_busy`, `round_robin`, or `first` for the old behaviour) and *Retries on another copy* (0–5).
- **Upgrade.** Nothing to do: no migration.

### SQL and OpenAPI tools
- **Let agents query a database, safely.** *Add SQL tool* on the Tools & MCP page saves a query you write, with `:name` placeholders the agent fills in. The agent never writes SQL. Every call is read-only, has a time limit and returns at most a set number of rows. PostgreSQL only.
- **Turn an API into tools.** *Import OpenAPI* reads an OpenAPI 3 document and adds one tool per operation. Each arrives disabled at HIGH risk until you review and enable it.
- **Only where you allow.** SQL and OpenAPI tools reach only hosts listed in `AGENTS__DATABASE_ALLOWED_HOSTS` and `AGENTS__OPENAPI_ALLOWED_HOSTS`. Both are empty by default, so nothing is reachable until you list hosts.
- **Fixed: agent tools now send their credentials.** Agent calls to REST and MCP tools went out without the tool's stored token. Workflow tool steps were not affected.
- REST tools now refuse cloud-metadata and link-local addresses, when saved and on every call, and no longer follow redirects. Tools you added yourself can now be deleted from the Tools page.
- **Upgrade.** Nothing to migrate. To use the new tools, set the allow-lists in `.env` and restart.

### Restricted documents
- **Keep a document to the people who should read it.** *Access* on a document's row restricts it to chosen users and teams. Everyone else, and the agents and workflows they run, no longer retrieve it or see it listed. Superusers still do. People who manage knowledge bases see it listed, marked *restricted*, but read it only if listed.
- **Restrict from the start.** Tick *Only me* when uploading, or send `restricted`, `users` and `teams` with the upload, so the document is never open while it indexes.
- Scheduled and webhook workflows read open documents only; a workflow someone starts reads what they can. New versions keep the same access. Every change is recorded in the audit log with its reason.
- **Upgrade.** Migration `b2d4f6a8c0e1` adds a column to `documents` and a `document_access` table. Existing documents stay open and need no re-indexing.

### Stop all agents
- **One switch for an incident.** *Stop all agents* on the Agents page stops every agent at once: no run starts or is approved, and running agents stop before their next step. This covers the console, the API, voice and `agent:` models in chat. *Resume agents* turns it back off. Runs waiting for approval keep waiting and can be approved after resuming.
- Everyone on the Agents page sees a banner while agents are stopped. The switch needs the right to change settings, asks for a reason and is recorded in the audit log. It is also on the Settings page under Security (`agents.kill_switch`).
- Workflows are not affected.
- **Upgrade.** Nothing to do: no migration.

### Audit log export and request IDs
- **Download the audit log.** *Export CSV* and *Export JSONL* on the Audit Log page download every record your filters match, newest first, up to 100,000. CSV opens safely in a spreadsheet; JSON Lines suits a log pipeline or SIEM. Each export is itself recorded in the audit log.
- **Follow one request.** Each record now shows the ID of the request that wrote it. Click it, or paste an ID from a log line, to see everything that request did.
- **Upgrade.** Nothing to do: no migration.

### Quota warnings
- **A warning share per quota.** Each quota can now warn at its own percentage of its daily or monthly budget (*Warn at* on the Usage & Quotas page). Leave it empty to keep the platform default of 80%.
- **Applications are told too.** A gateway response from a caller past the warning share carries an `X-Quota-Warning` header, for example `api_key; period=monthly; used=85%`, so an application can react before it is refused.
- **See it at a glance.** The quotas list shows each quota's warning share and whether it is *Near limit* or *Used up*.
- **Upgrade.** Migration `a1c3e5f7b9d2` adds one nullable column to `gateway_quotas`. Existing quotas keep warning at the platform default.

### GPU alerts
- **Hear about a hot or failing GPU.** A GPU that runs hot, nearly fills its memory or reports uncorrectable memory (ECC) errors now raises an alert on the Alerts page, one per GPU. It is sent to your notification channels like any other alert (source `gpu_threshold`) and clears itself when the GPU recovers.
- **Your thresholds.** Settings page, *GPU monitoring*: warning and critical temperature (83 °C and 90 °C by default), memory warning (98%), and a switch to turn GPU alerts off. Changes apply within a minute.
- **Upgrade.** Nothing to do: no migration. `WORKERS__GPU_ALERT_INTERVAL_SECONDS` (default 60) sets how often GPUs are checked.

### MCP server addresses checked
- **The same rules as backends.** Registering an HTTP or SSE MCP server now refuses addresses the platform must never call: cloud metadata endpoints, public AI services, URLs with a username or password in them, and anything other than `http`/`https`. Private addresses and compose service names are allowed as before. STDIO servers are not affected.
- The address is checked again on every call. A server registered earlier with a refused address shows *Unreachable* with the reason; delete it and register it again with a valid address.
- **Upgrade.** Nothing to do: no migration.

### PagerDuty channels
- **Page on-call directly.** A webhook channel can now use the *PagerDuty (Events API v2)* format: paste the integration's routing key and alerts open, update and resolve PagerDuty incidents, one per alert. *Send test* opens an incident and closes it at once.
- The routing key is stored encrypted and never shown or audited.
- **Upgrade.** Migration `f9b1d3e5a7c8` adds one column to `notification_channels`. Existing channels are unchanged.

### Branded email templates
- **Emails that look like yours.** Every notification email now uses a branded layout: your logo (or a monogram) and name in the header, a badge and a sentence saying what happened, the details, a button to the console, and your own footer. Digests show a count for each severity. Approver reminders link straight to the Approvals page.
- **Set it in the console.** Settings page, *Email branding*: brand name, accent colour (with a colour picker), tagline, footer and subject prefix. Changes apply from the next email. For a logo, set `NOTIFICATIONS__EMAIL_LOGO_PATH` to a PNG, JPEG or GIF of at most 200 KB; it is embedded in each email, never linked.
- **Preview before anyone gets one.** *Preview email* on the Alerts page shows each kind of email as people will receive it. It sends nothing and needs no mail server.
- The subject still starts with `[AI Platform]` by default, so existing mail filters keep working.
- **Upgrade.** Nothing to do: no migration.

### HTML notification emails
- **Easier to read in a mail client.** Alert, digest and approver reminder emails now include an HTML version alongside the plain text. It has a colour bar for the severity, the details in a small table, and a button to the Alerts page when `NOTIFICATIONS__CONSOLE_URL` is set. It loads nothing from the network: no images, fonts or scripts.
- If your mail gateway strips or quarantines HTML, set `NOTIFICATIONS__EMAIL_HTML=false` to send plain text only.
- **Upgrade.** Nothing to do: no migration.

### One digest when quiet hours end
- **No burst of messages in the morning.** When a channel's quiet hours end, chat webhooks and email channels now get what was held as one *[DIGEST]* message, with one line per alert, instead of one message per alert. Generic JSON webhooks still get one document per alert.
- The delivery list shows when each notification was held and which digest it went out in (`held_at`, `digest_id`).
- **Upgrade.** Migration `e7a9c1d3f5b6` adds two nullable columns to `notification_deliveries`.

### Team notification channels
- **Each team's alerts in its own chat.** A notification channel can now belong to a team. It then announces only that team's alerts: reminders about approvals for its agents and workflows, its token and cost budgets, and failed deployments of its models. Channels without a team still announce everything.
- On the Alerts page, choose the team under *Alerts about* on the channel form, or set `team_id` in the API. Alerts that concern a team show a team chip.
- A team cannot be deleted while it has a channel.
- **Upgrade.** Migration `d5f7a9c1e3b4` adds `team_id` to `alerts` and `notification_channels`. Existing channels and alerts have no team, so nothing changes until you set one.

### Turn off approver reminder emails for yourself
- **Your choice.** Anyone who can decide approvals can now stop the approver reminder emails for themselves. Click your name at the top of the console, then switch off *Email me reminders about approvals I can decide*. Or call `PUT /api/v1/users/me/preferences`. Emails already queued for you are cancelled. Alerts and channels still announce the approval.
- **Upgrade.** Migration `c3e5a7b9d1f2` adds `users.approval_reminder_emails`. It defaults to on, so nothing changes until someone switches it off.

### Move a resource between teams from the console
- **No more API calls to reassign.** If you may move a model, alias, API client, agent, tool, knowledge base or workflow, its team chip is now a button (*Shared* for a shared one). It opens a *Move to team* dialog. If the move is refused, for example because an agent would lose access to its model, the dialog says why.
- **Upgrade.** Nothing to do: console only, no migration.

### Reminders for approvals that never expire
- **Nothing waits forever unnoticed.** With approval expiry turned off (`agents.approval_ttl_hours` 0), set `agents.approval_reminder_waiting_hours` (Settings page, *Security*) to raise a reminder for each approval that has waited that long, such as *Approval for create_ticket (IT Helpdesk) has waited 3 days*. It reaches the Alerts page, subscribed channels and, when on, each approver's email, and clears once someone decides.
- **Upgrade.** Nothing to do: no migration, and it is off by default.

### Quiet hours for notification channels
- **Only critical at night.** A notification channel can now have quiet hours, for example 22:00 to 07:00: during them it sends only CRITICAL alerts. Everything else is held, not dropped, and sent when the window ends.
- **In your time zone.** The window is read in `UTC`, a fixed offset such as `+04:00`, or a zone name such as `Asia/Dubai` when the server has time zone data. A name it cannot resolve is refused when you save, not ignored at night.
- Set it on the channel form on the Alerts page, or with `quiet_start`, `quiet_end` and `quiet_timezone` in the API.
- **Upgrade.** Migration `b1d3f5a7c9e0` adds three columns to `notification_channels`. Existing channels have no quiet hours.

### Rotate a workflow webhook without dropped calls
- **Overlap window.** Rotating a workflow webhook can now keep the old token and signing secret working for a while (`overlap_minutes`, up to 7 days; the console offers 1 hour, 24 hours or 7 days), so callers can move to the new pair without failed calls. Each token is only accepted with its own secret. The workflow shows when the old token stops.
- The default is unchanged: without an overlap, the old pair stops at once.
- **Upgrade.** Migration `a9c1e3f5b7d8` adds three nullable columns to `workflows`.

### Timings in chargeback reports
- **Cost and speed side by side.** Chargeback rows and totals now carry mean and p95 latency, median and p95 time to first token, median output tokens per second, and how many streamed calls those come from. The page adds *p95 latency* and *First token (p50)* columns.
- **Exports too.** The CSV gains six columns at the end, after `estimate`, so existing spreadsheets keep their layout. A figure that was not measured is empty (or `null` in the API), never 0.
- **Upgrade.** Nothing to do: no migration.

### Reminder emails to each approver
- **Tell the people who can decide.** With `agents.approval_reminder_email_approvers` on (Settings page, *Security*; off by default), each approval reminder is also emailed to every person who could decide it: active accounts with `tool.approve` or `workflow.approve` that can see the agent's or workflow's team. A workflow run's starter is left out unless the step allows self-approval.
- **Once each, and never stale.** Each person is emailed once per approval, at most 25 people per approval. Sends are retried like notification channels, and one not yet sent when the approval is decided or expires is cancelled.
- **Upgrade.** Migration `f7b9d1e3a5c6` adds the `approver_reminders` outbox. Needs the notification mail server (`NOTIFICATIONS__SMTP_HOST`); nothing changes until the setting is turned on.

### Reranking calls accounted
- **Reranks on the books.** Every rerank a knowledge search, agent or workflow retrieval, playground query or RAG evaluation runs now writes a usage row (`<purpose>.rerank`), charged to the person and to the bases' team, and counted toward team and model budgets like embeddings. Before, reranking was invisible to usage, budgets and chargeback.
- **Real token counts only.** Tokens are what the engine reports: vLLM's `/v1/rerank` does; Hugging Face TEI does not, and then the call is recorded with 0 tokens rather than a guess.
- With `gateway.enforce_platform_budgets` on, a rerank over budget is skipped and the results stay in vector order with a warning.
- **Upgrade.** Nothing to do: no migration.

### Dashboard looks back 90 days
- **Longer ranges.** The dashboard's range now offers *Last 30 days* and *Last 90 days* (`window_hours` up to 2160, also on `GET /monitoring/overview`). Before, it stopped at 7 days.
- **A bar per day past a week.** Beyond 7 days the traffic chart has one bar per UTC day instead of one per hour. `gateway.bucket` in the response says which; the series' fields are unchanged.
- **Upgrade.** Nothing to do: no migration.

### Signed workflow webhooks
- **Prove the body and the moment.** Enabling or rotating a workflow webhook now also issues a signing secret. A caller signs `<timestamp>.<body>` with HMAC-SHA256 and sends `X-Workflow-Timestamp` and `X-Workflow-Signature: sha256=…`, as for Stripe or GitHub webhooks. A changed body, a timestamp more than 5 minutes off, or a signature already used is refused.
- **Opt in per workflow.** *Require signed calls* (or `{"require_signature": true}` when rotating) refuses unsigned calls; otherwise unsigned calls still work but a bad signature does not. The console shows a ready `curl` + `openssl` example.
- **Upgrade.** Migration `e6a8c0d2f4b5`. Existing webhooks keep working unsigned; rotate one to get its signing secret.

### Tools can belong to a team
- **Team tools.** A tool can now have an owning team, like a model or knowledge base: register it with `team_id`, or move it with `PUT /scope` (`resource_type: "tool"`). Other teams do not see it, and only that team's agents may be granted it; a shared agent may use only shared tools. Checked when an agent is saved or moved and at every run.
- **Fix: workflows can be moved between teams.** `PUT /scope` refused `resource_type: "workflow"` with 422, although it was documented.
- **Upgrade.** Migration `d5f7b9c1e3a4` adds `tools.team_id`. Every existing tool stays shared, so nothing changes until a tool is moved. Moving a tool into a team stops shared agents that use it at their next run, with a message naming the tool.

### A running agent stops at the budget
- **No more runs that overspend.** With platform budgets enforced, every model call an agent run makes is now checked before it is made, counting the run's own tokens so far. A run that reaches its team's or model's budget stops there: FAILED with *Stopped before the next model call* and the budget's message, also shown in its trace. Before, only the start was checked.
- Runs resumed after an approval are checked before their first call. `agent:` models called through `/v1` are unchanged (the gateway admits them once).
- **Upgrade.** Nothing to do: no migration; it applies only while `gateway.enforce_platform_budgets` is on.

### Passkeys in the developer portal and voice assistant
- **Sign in with a passkey** now also on the developer portal and the voice assistant: no username, password or code. At the code step both also offer *Use a passkey instead* when the account has one.
- The same passkeys work everywhere: the three pages share one origin and now one script (`/js/passkey.js`). Passkeys are still added, and browsers trusted, in the console.
- **Upgrade.** Nothing to do: no migration, no setting.

### Documents a budget refused are retried automatically
- **No more manual re-ingest after a budget resets.** With platform budgets enforced, a document whose embedding a used-up team or model budget refused is still marked FAILED with the reason, and now also says when it will be retried: just after the budget resets. The ingestion worker then queues it again by itself.
- **Bounded.** A budget still used up refuses it again with a new time, so it costs one check per reset. Other failures are not retried. Re-ingesting by hand cancels the pending retry.
- **Upgrade.** Migration `c4e6a8b0d2f3` adds `documents.retry_at`. See [docs/gateway-policy.md](docs/gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls).

### Off-host backup copies to S3-compatible storage
- **Copies to an object store.** Set `OPS__BACKUP_COPY_S3_ENDPOINT`, `_BUCKET`, `_ACCESS_KEY` and `_SECRET_KEY` in the host's `.env` and the ops runner uploads every backup to S3-compatible storage (a second MinIO, Ceph RGW, Garage or AWS) instead of a directory. Retries, the *Off-host backup copy missing* alert and retention work as for a directory.
- **Checked on arrival.** Each upload is signed over its SHA-256 and the store rejects a mismatch; a `.complete` marker uploaded last decides what counts as copied, so a half-finished upload never does.
- **Keys stay on the host**; the console shows the bucket, prefix and endpoint only. No new dependency: the client is standard-library Python.
- **Upgrade.** No migration. Restart the ops runner after setting the variables. See [docs/backup.md](docs/backup.md#copies-to-s3-compatible-storage).

### Sign in with a passkey alone
- **Passwordless sign-in in the console.** *Sign in with a passkey* asks for no username, password or code: the browser offers your passkeys for this site and you unlock one with your device's PIN or biometric. That is two factors in one step, and it cannot be phished.
- **Safe by construction.** The challenge names no account, so it reveals nothing about which exist; user verification is required; each challenge works once; locked and disabled accounts are refused; refused attempts count toward the address throttle.
- **Passkeys added from now on** are requested as discoverable, which passwordless sign-in needs. One added before may need removing and adding again to sign in with it alone; it keeps working at the code step either way.
- **Upgrade.** No migration. See [docs/security.md](docs/security.md#signing-in-with-a-passkey-alone).

### Sign-in check after repeated failures
- **A proof of work instead of a CAPTCHA.** After 10 failed sign-ins from one address (`AUTH__LOGIN_CHALLENGE_AFTER`, 0 = off), signing in from it needs a small SHA-256 proof of work first: about a second in a browser, doubling every five further failures up to about 15 seconds. Nothing leaves the site; hosted CAPTCHAs need the internet.
- **Automatic in the platform's pages.** The console, developer portal and voice assistant solve it on their own and say so. API clients that sign in with a password get 401 `challenge_required` with the challenge and must resend with `challenge: {token, solution}`; API keys never meet it.
- **Per address, checked before the password**, so it reveals nothing about accounts and an unsolved attempt tests no password.
- **Upgrade.** No migration. Settings `AUTH__LOGIN_CHALLENGE_AFTER` (10) and `AUTH__LOGIN_CHALLENGE_BITS` (20). See [docs/security.md](docs/security.md#sign-in-hardening).

### Blocked models' deployments are retried
- **The deployment worker finishes the job.** About once a minute it stops any deployment of a blocked model that is still live: one whose node was unreachable when the model was blocked, or one that was still being created. Audited as `deployment-worker`.
- **Alert while it cannot.** *Blocked model still running* (WARNING) stays open for each deployment the worker cannot stop, and clears once it is stopped.
- **Upgrade.** No migration.

### Voice speech calls attributed
- **Voice usage has an owner.** The voice assistant's speech-to-text and text-to-speech calls were recorded with no user or team. They are now recorded for the person speaking and charged to the team of the agent they talk to, so they appear under that person and team on the Usage page, in chargeback and against team budgets.
- **Nothing new is refused by default.** Model quotas apply as before. A team budget refuses voice only when `gateway.enforce_platform_budgets` is on.
- **Upgrade.** No migration.

### Blocking a model stops its deployments
- **Blocking a model now stops its running deployments.** A blocked model is never served, so its containers were only holding GPUs. Each stop is audited. The Edit dialog offers *Stop its running deployments too* (ticked) when Blocked is chosen; the API takes `"stop_deployments": false` to keep them.
- **An unreachable node does not undo the block.** A deployment that could not be stopped is listed in the response's `stop_failed` and keeps running (the gateway still refuses it); stop it from the Deployments page later.
- **API.** `POST /models/{id}/approval` returns the model plus `stopped_deployments` and `stop_failed`. Existing fields are unchanged. No migration.

### Lockout for directory accounts
- **LDAP accounts are locked like local ones.** After `AUTH__LOCKOUT_THRESHOLD` wrong passwords made through the platform, a directory account is locked for `AUTH__LOCKOUT_MINUTES`, with the same audit entry, alert and *Unlock* button on the Users page.
- **While locked, the password is not sent to the directory**, so guessing through the platform can no longer trip the directory's own lockout for the real person.
- **Upgrade.** No migration and no new setting: the existing lockout settings now cover directory accounts. Single sign-on (OIDC) accounts are unaffected. See [docs/security.md](docs/security.md#sign-in-hardening).

### Budgets can refuse the platform's own calls
- **Opt in.** A new runtime setting, `gateway.enforce_platform_budgets` (Settings page, *Gateway*, off by default). When it is on, agent runs started in the console or API, workflow LLM steps, ingestion, knowledge search, memory and other platform embeddings are refused once their team's or model's token budget is used up, as `/v1` calls are. When it is off, nothing changes: these calls are counted against budgets and never refused.
- **What a refusal looks like.** Starting an agent run answers 429 with the budget's message and `Retry-After`. A workflow step fails with the message. A document being ingested is marked failed with it. Agent retrieval and memory continue without context.
- **Upgrade.** No migration, and nothing changes until the setting is switched on. See [docs/gateway-policy.md](docs/gateway-policy.md#enforcing-budgets-on-the-platforms-own-calls).

### Backup retention by age and off-host copies
- **Keep backups for N days.** A new runtime setting, `ops.backup_keep_days` (Settings page, 0 = no age limit). With `ops.backup_keep` also set, a backup is deleted only when it is beyond the count *and* past the age. The newest complete backup is never deleted.
- **Off-host copies.** Set `OPS__BACKUP_COPY_DIR` in the host's `.env` (an NFS or SMB share, or a USB disk) and restart the runner. Each new backup is copied there and compared file by file before it counts. A failed copy is retried once, raises an `Off-host backup copy missing` alert, and never fails the backup. Retention prunes the copies with the backups. The Backup & Restore page shows the directory and whether the newest backup is copied.
- **Docs.** `docs/architecture.md` no longer says LangGraph runs agents: the platform's own runtime does, and the page says why.
- **Upgrade.** Migration `b3d5f7a9c1e2` allows the new `backup.copy` job. The new `scripts/ops_runner.py` ships in the bundle; restart the runner after upgrading (and after setting `OPS__BACKUP_COPY_DIR`) so it runs it. See [docs/backup.md](docs/backup.md#off-host-copies).

### Approval reminders
- **A warning before an approval expires.** An agent tool call or workflow step whose approval will expire within `agents.approval_reminder_hours` (default 24, Settings page; 0 = off) raises a WARNING alert naming the tool and agent, or the workflow step, and the time left. It appears on the Alerts page and clears once someone decides or the approval expires.
- **Notifications.** A new alert source, `approvals`: subscribe a webhook or email channel to it to tell approvers. Channels that send every source get reminders without a change.
- **Upgrade.** No migration. With the defaults, approvals already waiting more than 48 hours raise reminders at the first check after upgrading. See [docs/agents.md](docs/agents.md#reminders).

### Embedding calls accounted
- **Ingestion, search and memory are counted.** Each embedding the platform makes now writes a usage row with the tokens the model server reported. That covers ingesting a document, knowledge base search, the retrieval playground, agent and workflow retrieval, RAG evaluations, and storing or recalling memories. Ingestion is charged to the uploader and the knowledge base's team, searches to the person searching (or the run's starter), and memory to its person and the agent's team. Endpoints are `knowledge.ingest`, `knowledge.search`, `retrieval.playground`, `agent.retrieve`, `workflow.retrieve`, `rag.eval`, `memory.write`, `memory.search` and `knowledge.probe`, with `kb:<name>` or `memory` as the requested model.
- **Quotas.** These tokens count toward team and model token budgets but never refuse an ingestion or a search.
- **Upgrade.** No migration. Usage, chargeback and the dashboard's gateway figures rise by what embeddings had been spending unrecorded, which on an embedding deployment is most of its work. See [docs/gateway-policy.md](docs/gateway-policy.md#embeddings).

### Passkeys and trusted browsers
- **Passkeys as a second factor.** With two-factor authentication on, people can add security keys or their device's screen lock (Windows Hello, Touch ID, Android) on the account dialog and use them instead of the six-digit code. A passkey never replaces the password. A failed passkey counts toward the lockout like a wrong code.
- **Trust this browser.** The code step can trust the browser for `auth.mfa_remember_days` days (Settings page, default 30; 0 turns it off). The password is still checked. Signing out everywhere, changing the password or an administrator's 2FA reset forgets every trusted browser. The account dialog lists them, with Forget and Forget all.
- **Needs a host name over HTTPS.** Passkeys are bound to the host in `PLATFORM__PUBLIC_BASE_URL` (override with `AUTH__WEBAUTHN_RP_ID` / `AUTH__WEBAUTHN_ORIGINS`). A platform reached by IP address offers no passkeys and says so; codes work as before.
- **Not in the portal or voice assistant**, which still ask for the code.
- **Upgrade.** Migration `a8c4e2f6b9d1` adds `user_passkeys` and `trusted_devices`. Four new pinned Python packages: `webauthn` 3.0.1, `cbor2`, `pyopenssl` and `pyasn1-modules`; rebuild the backend image. See [docs/security.md](docs/security.md#passkeys-and-trusted-browsers).

### Platform model calls accounted
- **Agent runs everywhere.** Agent runs started in the console, through `POST /agents/{id}/execute` or by voice now write a usage row (`endpoint` `agent.run`, `requested_model` `agent:<slug>`), as do the tokens a run spends after an approval. They appear on the Usage page, in chargeback and in budgets, attributed to the person who started the run and the agent's team.
- **Workflow LLM steps.** Each LLM step writes a usage row (`workflow.llm`, `workflow:<name>`) for the run's starter and the workflow's team.
- **Not double counted.** `agent:<slug>` calls through `/v1` are still recorded once, by the gateway, against the API key.
- **Quotas.** These tokens count toward team and model token budgets, so later `/v1` calls see them, but agent runs and workflow steps are not refused by a budget.
- **Upgrade.** No migration. Usage totals, chargeback and the dashboard's gateway figures rise by what agents and workflows had been spending unrecorded. See [docs/gateway-policy.md](docs/gateway-policy.md#the-platforms-own-model-calls).

### Team scoping finished
- **Agents and workflows use what their own team can see.** An agent's or workflow's model and knowledge bases are now checked against the resource's own team, not only against the person saving it. A shared agent or workflow may use only shared models and knowledge bases. It is checked on save (422), on `PUT /scope` moves (409), when an agent run starts (409, no run created) and in every workflow LLM and retrieval step. Before, an agent saved earlier kept using a model after the model moved to another team.
- **Reads follow scope.** `/traces/{id}`, voice sessions, memory search, memory entries, `/conversations/{id}`, the dashboard's model, deployment and gateway figures, and the monitoring overview count only what the caller's teams can see. Memory follows its agent's team. Definitions import respects the importer's teams.
- **Behaviour change.** A shared agent or workflow that refers to a team's model or knowledge base fails at its next run, with a message saying to move it into that team or make the resource shared. Team-limited callers of `POST /memory/forget-all` erase only their teams' and shared agents' entries; erasing everything still needs `scope.all`.
- **Upgrade.** No migration. See [docs/teams.md](docs/teams.md#what-an-agent-or-workflow-may-use).

### Approval expiry and agent usage
- **Approvals expire.** A tool call or workflow step waiting for approval longer than `agents.approval_ttl_hours` (default 72, Settings page; 0 = never) expires, and its run fails with a message saying nobody decided. Expiry is audited as the scheduler (`TOOL_APPROVAL_EXPIRED`, `WORKFLOW_APPROVAL_EXPIRED`) and is never recorded as a rejection. A workflow does not take its rejected branch.
- **Agent calls are accounted.** `agent:<slug>` calls through `/v1` now write a usage row (`agent:<slug>` as the requested model), so they appear on the Usage page, in chargeback and in budgets. They are admitted against API key, team, end-user and model quotas.
- **Behaviour change.** A key over its token budget or concurrency limit now gets `429` for agent calls as well. Before this release, agent calls bypassed quotas. Runs that had been waiting for approval for more than 72 hours fail at the first check after upgrading; to keep them waiting, set `agents.approval_ttl_hours` to 0 before upgrading.
- **Upgrade.** No migration. See [docs/agents.md](docs/agents.md#expiry).

### Gateway timings
- **Time to first token and throughput.** Each streamed chat call through `/v1` stores how long the first token took and how long the answer took to generate. The dashboard has a *First token (p50)* card with p95 and output tokens per second. Usage & Quotas adds First token p50/p95, p95 latency and Tok/s per model.
- **Measured, not estimated.** Buffered calls have no first token to time, so their figures are empty, and "—" is shown when nothing was streamed. Throughput leaves out disconnects, failures and one-token answers.
- **Prometheus.** New histograms `ai_platform_gateway_request_duration_seconds` and `ai_platform_gateway_output_tokens_per_second`, with panels on the Grafana overview.
- **Fixed.** A runtime's role-only first chunk was counted as the first token, which made TTFT look faster than it was.
- **Upgrade.** One migration (`f7b2d4a6c9e3`, two nullable columns, no backfill). The API changes only add fields. See [docs/gateway-policy.md](docs/gateway-policy.md#timings).

### Two-factor authentication
- **Authenticator app codes.** Local accounts can turn on TOTP (RFC 6238), which works with any standard authenticator app. The QR code is drawn on the server, so nothing loads from the internet. Ten single-use recovery codes are shown once.
- **Two-step sign-in.** With MFA on, `/auth/login` returns `mfa_required` and a short-lived step token instead of tokens; `/auth/mfa/verify` finishes the sign-in. Wrong codes count toward the account lockout, and a code works only once.
- **Policy.** The runtime setting `auth.mfa_required` (`off`, `admins`, `all`) on the Settings page. Required users set MFA up at their next sign-in, and cannot turn it off.
- **Everywhere you sign in.** The admin console has set-up, the code step and an account dialog. The developer portal and the voice assistant ask for the code. The Users page shows who has 2FA and can reset it for a lost phone (audited, signs the user out).
- **Upgrade.** One migration (`e6a1c3f5b8d2`) and one new pinned dependency, `segno==1.6.6` (pure Python, hashed in `requirements.txt`). Nothing changes until someone turns MFA on or the policy is set. API clients that sign in as a user should check `mfa_required`. See [docs/security.md](docs/security.md#two-factor-authentication).

### Auth hardening
- **Account lockout.** 10 wrong passwords in a row lock a local account for 15 minutes (`AUTH__LOCKOUT_THRESHOLD`, `AUTH__LOCKOUT_MINUTES`). A locked account gets the same message as a wrong password, and its password is not checked. The lock is audited, raises an alert, and can be lifted early on the Users page.
- **Per-address throttle.** 30 failed sign-ins from one address in 15 minutes refuse it with `429` (`AUTH__LOGIN_IP_*`). This catches password spraying, and covers directory accounts too. It is audited and raises an alert. It fails open while Valkey is down.
- **Real sign-out.** `POST /auth/logout` revokes the session's tokens. *Sign out everywhere* is available as `POST /auth/logout-all` and as a Users page action. Password changes, password resets and disabling an account sign the user out everywhere.
- **Refresh rotation.** Each refresh token works once. One reused after it was exchanged signs the account out everywhere and raises a CRITICAL alert.
- **Ops runner.** Wrong runner tokens are audited and throttled per address (`OPS__RUNNER_MAX_FAILURES`, `OPS__RUNNER_FAILURE_WINDOW_SECONDS`), which closes threat model item 12's gap.
- **Failed sign-ins are finally counted.** `failed_login_count` used to roll back with each failed request.
- **Upgrade.** One migration (`d5f9b2a3e7c1`). Existing tokens keep working. Alert sources `auth` and `ops_runner` can be picked in notification channels. See [docs/security.md](docs/security.md#sign-in-hardening).

### Alert notifications
- **Notification channels.** Send alerts to webhooks (Mattermost, Slack, Teams, Rocket.Chat, ticketing, SOC) and to email lists on the site's own mail server. Managed on the Alerts page with the new `notification.manage` permission (ADMIN and SUPER_ADMIN).
- **Once per change.** Each alert is announced when raised, when it escalates, and when it is resolved. Repeats are not resent. Each channel has a minimum severity and a source filter.
- **Reliable delivery.** An outbox with retries and back-off, a delivery history, a *Send test* button, and an in-app alert when a channel keeps failing.
- **Secrets stay secret.** Webhook URLs, signing secrets and authorization headers are stored encrypted and never shown again. Messages can be signed with HMAC-SHA256 (`X-AIP-Signature`). URLs go through the outbound URL policy, and redirects are never followed.
- **Upgrade.** One migration (`c4e8a1f2d6b9`). Run `make seed-rbac` (the console upgrade does it) to grant `notification.manage`. Nothing is sent until a channel is added. For email, set `NOTIFICATIONS__SMTP_HOST` and the related settings in `.env`. See [docs/notifications.md](docs/notifications.md).

### Budget alerts and scheduled backups
- **Token quota alerts.** In-app WARNING at 80% (`gateway.budget_warning_percent`) and CRITICAL at 100% of each daily and monthly quota, per subject. They clear themselves when usage resets.
- **Cost budgets.** Monthly budgets in the chargeback currency, for the whole platform, a team or an API key, each with its own warning share. They raise alerts but never block traffic. New routes under `/api/v1/chargeback/budgets`, audited. Budgets card on the Chargeback page.
- **Scheduled backups.** `ops.backup_interval_hours` makes the platform queue backups for the ops runner, as audited jobs requested by `scheduler`. Alerts appear when a backup fails or is overdue.
- **Retention.** `ops.backup_keep` deletes the oldest complete backups beyond N through a new `backup.prune` runner job. The newest backup and anything without a manifest are never deleted.
- **Upgrade.** One migration (`9a6f2c4e1b57`). Both features are off until configured: no budgets exist and the backup interval is 0. Restart `make ops-runner` after upgrading so it knows the new job kind.

## 0.2.0: the expansion release

This release builds on 0.1.0. It is additive:

- existing APIs keep their paths and shapes;
- the eight migrations backfill existing rows so behaviour does not change;
- `NULL` in every new `team_id` column means "shared", so an upgrade removes nobody's access.

### Inference backends and alerts
- **Inference backend registry**: vLLM, Ollama and OpenAI-compatible endpoints. Each can be tested, can discover its models and can register them. Models can link to a backend (`models.backend_id`).
- **One outbound URL policy** (`core/url_policy.py`), shared by backends and the model registry.
- **In-app alerts**. A backend health job raises and clears CRITICAL alerts, and a failed deployment raises one.
- **New pages**: Backends, Alerts, and an alert badge in the console.

### Gateway policy
- **Token quotas** per minute, day and month, plus **maximum concurrency**. They can be set per API key, per trusted end user or per model, with `*` defaults.
  - Usage is counted in Valkey and seeded from `usage_records`.
  - If Valkey is down, inference is never blocked.
- **Per-backend circuit breakers**. An open circuit fails fast and returns `Retry-After`.
- **One-hop fallback aliases**. A fallback is recorded in `usage_records.fallback_from`.
- **Backend credentials**: the gateway sends a linked backend's API key, and only to that backend's URL.
- **New page**: Usage & Quotas.

### Teams and resource scope
- **Teams** with member and owner roles.
- **Team scope**: a nullable `team_id` on models, aliases, API clients, agents, knowledge bases and usage.
- **One visibility rule**: a resource is visible if it is shared, belongs to a team you are in, or you hold `scope.all`. Anything else answers 404. The gateway enforces the rule too.
- **Moving resources**: `PUT /api/v1/scope` moves a resource between teams.
- **Team quotas.**
- **New role** `KNOWLEDGE_ADMIN`.

### Model governance
- **Approval status** for models: approved, experimental, deprecated or blocked.
  - Existing models are backfilled as *approved*. **New registrations start as *experimental*.**
  - Blocked and deprecated models refuse new deployments.
  - The gateway does not serve blocked models.
- **Model metadata**: licence, source, publisher, family and tags.
- **Deployment dry run**: `POST /models/{id}/deploy/validate`.
- **Deployment revisions** with rollback, and a runtime image allowlist (`MODELS__RUNTIME_IMAGE_ALLOWLIST`).
- **Live deployment logs** over server-sent events.

### Retrieval depth
- **Per-knowledge-base retrieval settings**: reranker, top-k, number of rerank candidates and minimum score. If the reranker fails, results fall back to vector order.
- **Document versions**. A new version is published only once it is indexed, and documents can be re-ingested.
  - Re-chunking now removes the previous vectors instead of leaving them orphaned.
- **Retrieval Playground**, plus evaluation sets that score hit rate, recall, precision and MRR.

### Workflows
- **A workflow engine on Postgres**:
  - nodes for LLM, retrieval, tool, HTTP, condition, transform, approval, delay and end;
  - retries, timeouts and error branches;
  - a frozen definition per run.
- **Lifecycle**: versions with draft, publish, pause and archive.
- **Triggers**: manual, API, cron and webhook.
- **Approvals**: approval steps, and a pause before HIGH and CRITICAL tools.
- **New page**: Workflows.

### Operations console
- **Audited operations jobs**: backup, verify, restore, bundle verification, upgrade and rollback.
  - A host runner authenticated by a token (`scripts/ops_runner.py`) runs them.
  - The control plane still has no Docker socket.
- **Upgrade preflight.** Destructive jobs require a typed confirmation and a reason.
- **Signed bundle manifests** (ECDSA P-256), a **CycloneDX SBOM** and a licence list.
- **Diagnostics**: version inventory, redacted effective configuration and a support bundle.
- **New pages**: Backup & Restore, Offline Updates, Diagnostics.

### Dashboard and settings
- **Unified dashboard**: one page with fleet, GPUs, models, gateway, dependencies, backends, alerts, knowledge, agents, workflows and recent activity.
- **Settings page**:
  - runtime settings, editable without a restart and audited;
  - deployment settings, read-only and redacted.

### Chargeback
- **Dated cost rates** per model, with a `*` default.
- **Cost reports** by team, API key, user, end user and model.
  - GPU cost is shared out by token share, and idle capacity is shown.
  - CSV export is safe from spreadsheet formula injection.
- **New page**: Chargeback. Currency comes from `CHARGEBACK__CURRENCY`.

### Connector catalogue
- **A catalogue of MCP connectors** from `mcp/manifests/`, validated against one set of rules.
  - Install registers a connector and discovers its tools, which arrive disabled at HIGH risk.
  - "Apply suggested risk" only relabels tools nobody has reviewed yet.
- **New connectors**: time, sqlite, prometheus and redis.
- **New page**: Connector Catalogue.

### Release readiness
- **Fixed: writes were committed after the response was sent.** FastAPI closes request-scoped dependencies after the response, so a client could get 200 before its change was committed. If that commit then failed, the change was lost after the client had been told it succeeded.
  - Every API route now commits before responding (`app/api/routing.py`), and a failed commit returns an error.
  - Streaming responses still commit when the stream ends.
  - A test fails if a router does not use `CommittingRoute`.
- **Fixed: upgrades did not grant new permissions.** Neither `upgrade.sh` nor the console's upgrade job reconciled role grants, so new pages answered 403 for every administrator except superusers.
  - The new `python -m app.utils.cli seed --rbac-only` reconciles permissions and system-role grants without creating or changing accounts.
  - The console upgrade runs it after migrating, and `upgrade.sh` prints it as a step.
- **Audit gaps closed.** The API tests were run with a probe that recorded which successful writes produced an audit row. These did not, and now do:
  - creating and deleting skills (`SKILL_CREATED`, `SKILL_DELETED`), since a skill's instructions go into agent prompts;
  - operator GPU reservations and releases (`GPU_RESERVED`, `GPU_RELEASED`);
  - importing a model from disk (`MODEL_IMPORTED`).

  Writes to evaluation-set questions are still not audited. They are scoring data and do not change what is served.
- **Telemetry off, stated explicitly**:
  - Grafana Alloy now runs with `--disable-reporting`; before this it sent anonymous usage statistics by default.
  - Open WebUI's `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK` and `ANONYMIZED_TELEMETRY` are set in compose rather than inherited from the image.
- **Version** 0.2.0. Images built from this tree are tagged `0.2.0` unless `PLATFORM_VERSION` says otherwise.

## 0.1.0

The baseline release. It covers:

- the control plane, local, OIDC and LDAP authentication with RBAC, and the audit log;
- the model registry and deployments;
- the OpenAI-compatible gateway and GPU and node monitoring;
- RAG, agents and MCP tools, chat and voice;
- observability, and the air-gapped bundle with install, upgrade and rollback.
