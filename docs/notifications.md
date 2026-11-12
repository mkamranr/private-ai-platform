# Alert notifications (E13)

In-app alerts (E1) only help if someone is looking at the console. A **notification
channel** sends alerts somewhere people already look:

- a **webhook**, for example Mattermost, Slack, Microsoft Teams or Rocket.Chat incoming
  webhooks, a ticketing system, a SOC collector or an automation endpoint;
- an **email** list, through the site's own mail server.

Channels are managed on the **Alerts** page, under *Notification channels*, by holders of
`notification.manage`. ADMIN and SUPER_ADMIN have it.

Nothing is sent until you add a channel. The platform keeps no Internet dependency: a
channel points at a host you choose, normally inside your own network.

## What is sent

For each enabled channel, an alert produces at most three messages:

| Event | When |
|---|---|
| `raised` | an alert opens at or above the channel's minimum severity |
| `escalated` | an open alert the channel announced is raised again at a higher severity, e.g. WARNING → CRITICAL |
| `resolved` | an alert the channel announced is resolved, by the platform or by a person, if *Also say when an alert is resolved* is on |

Each channel also chooses:

- **Minimum severity:** critical only, warning and critical (the default), or everything.
- **Sources:** which kinds of alert to send. Leave them all unticked to send every source.
  Today's sources are `backend_health`, `deployment`, `quota_budget`, `cost_budget`,
  `backup_schedule` and `notifications`.

**Not sent:**

- **Repeats.** An alert that recurs every minute is one message, not sixty. The database
  refuses a second delivery with the same channel, alert and severity.
- **Downgrades** from CRITICAL to WARNING.
- **Alerts raised before the channel was created or last turned on.** Adding a channel does
  not replay every open alert at once.
- **Blips.** An alert that opens and clears between two dispatcher runs (30 seconds by
  default) is never sent.
- **A channel's own failures** are never sent to that channel, though other channels
  receive them.

## How delivery works

Raising an alert never waits on a mail server or a webhook. A background job
(`notification_dispatch`, every `WORKERS__NOTIFICATION_INTERVAL_SECONDS`, default 30)
queues one delivery per channel and event in `notification_deliveries`, then sends the
deliveries that are due.

- **Retries.** A failed send is retried after 30 s, 2 min, 10 min, 30 min and 2 h, up to
  `NOTIFICATIONS__MAX_ATTEMPTS` (default 5), and then marked failed.
- **One slow receiver doesn't hold up the rest.** When a channel fails during a run, its
  other messages wait for the next run, and other channels carry on.
- **Channel health.** After three failures in a row, the platform raises a WARNING alert,
  *Notifications to … are failing*, with the last error. It clears itself at the next
  successful send, and when the channel is turned off or removed.
- **Turning a channel off** cancels its queued messages. Turning it back on starts from
  that moment.
- **History.** *Recent notifications* on the Alerts page lists messages that were sent,
  queued, retried or given up on, with the last error. Finished deliveries are kept for 30
  days.
- **Test.** *Send test* sends a message straight away and shows the receiver's answer. It
  is recorded as a delivery and in the audit log.

Only one control-plane instance runs the workers (see `WORKERS__ENABLED`). Deliveries are
locked with `FOR UPDATE SKIP LOCKED`, so a second instance would not double-send, but it
is not the tested setup.

## Webhooks

The platform sends one `POST` per message, with `Content-Type: application/json`.

**Message formats**

- **Chat text:** `{"text": "..."}`, which Mattermost, Slack, Teams and Rocket.Chat
  incoming webhooks all accept. The text gives the severity and title, the message, the
  source and time, and a link to the Alerts page if `NOTIFICATIONS__CONSOLE_URL` is set.
- **JSON document:** for systems that parse it. The field names are a contract
  (`version: 1`):

```json
{
  "version": 1,
  "event": "raised",
  "delivery_id": "2f0c…",
  "sent_at": "2026-11-12T09:30:00+00:00",
  "alert": {
    "id": "8d1e…", "fingerprint": "backend_unhealthy:…", "severity": "CRITICAL",
    "state": "ACTIVE", "source": "backend_health",
    "title": "Inference backend 'gpu-01' is unreachable", "message": "…",
    "occurrences": 3, "resource_type": "inference_backend", "resource_id": "…",
    "raised_at": "…", "resolved_at": null
  },
  "console_url": "https://ai.example.internal/#alerts"
}
```

**Headers** on every request:

- `User-Agent: private-ai-platform-notifier`
- `X-AIP-Event`: raised, escalated, resolved or test.
- `X-AIP-Delivery`: the delivery id. It is the same on retries, so a receiver can drop
  duplicates.

**Signing.** With a signing secret (at least 16 characters), each request also carries:

- `X-AIP-Timestamp`: Unix seconds.
- `X-AIP-Signature: sha256=<hex>`: HMAC-SHA256 with the secret, over the timestamp, a dot,
  and the exact request body.

A receiver should recompute the signature, compare it in constant time, and reject a
timestamp more than a few minutes old so a captured request can't be replayed:

```python
import hashlib, hmac, time

def verify(secret: bytes, headers, body: bytes) -> bool:
    ts = headers["X-AIP-Timestamp"]
    if abs(time.time() - int(ts)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-AIP-Signature"])
```

**Authorization header.** For receivers that want a token, such as `Bearer …` or Splunk
HEC's `Splunk …`, set the whole header value.

**The address is a secret.** Chat webhook URLs carry their credential in the path, so:

- the URL, signing secret and authorization value are stored Fernet-encrypted
  (`SECURITY__ENCRYPTION_KEY`);
- the API and the console only ever show the scheme, host and port;
- the audit log records the same masked address, and never a secret;
- error messages never repeat the URL.

**URL policy.** Addresses go through the same outbound policy as inference backends
(`core/url_policy.py`), both when saved and again at every send. Refused:

- schemes other than http(s);
- credentials in the URL;
- link-local and cloud metadata addresses;
- public AI services.

Redirects are never followed, so a receiver can't bounce a request elsewhere.

**Answers.** Any 2xx is success. A 3xx counts as a failure, because redirects are not
followed. Anything else is retried, with the status and the start of the body shown as
the error.

## Email

Email goes through the site's mail server, configured in `.env`. The password is a secret
and never goes in the database:

| Setting | Default | Meaning |
|---|---|---|
| `NOTIFICATIONS__SMTP_HOST` | unset | the mail server; email channels fail clearly until it is set |
| `NOTIFICATIONS__SMTP_PORT` | 587 | |
| `NOTIFICATIONS__SMTP_SECURITY` | `starttls` | `starttls` (587), `ssl` (implicit TLS, 465) or `none` (a relay on a trusted network only) |
| `NOTIFICATIONS__SMTP_USERNAME` / `NOTIFICATIONS__SMTP_PASSWORD` | unset | login, if the server needs one |
| `NOTIFICATIONS__SMTP_FROM` | `ai-platform@localhost` | the sender address |
| `NOTIFICATIONS__SMTP_VERIFY_TLS` | true | turn off only for an internal relay with a self-signed certificate |
| `NOTIFICATIONS__SMTP_TIMEOUT_SECONDS` | 10 | |

- Messages are plain text, with the subject `[AI Platform] [CRITICAL] <title>`.
- A channel has up to 20 recipients; use a distribution list for more.
- If the server refuses some recipients but not all, the message counts as sent and the
  result says how many were refused.

## Other settings

| Setting | Default | Meaning |
|---|---|---|
| `NOTIFICATIONS__CONSOLE_URL` | unset | the console's address, e.g. `https://ai.example.internal`; messages link to `/#alerts` |
| `NOTIFICATIONS__MAX_ATTEMPTS` | 5 | attempts per message before it is marked failed |
| `NOTIFICATIONS__BATCH_SIZE` | 50 | messages sent per run, across all channels |
| `WORKERS__NOTIFICATION_INTERVAL_SECONDS` | 30 | how often messages are queued and sent |

The metric `ai_platform_notifications_total{kind, outcome}` counts send attempts by
channel kind (`webhook` or `email`) and outcome (`sent` or `failed`).

## API

Under `/api/v1/notifications`. Every route needs `notification.manage`.

| Method and path | Notes |
|---|---|
| `GET /channels` | channels with health and queued count, plus `smtp_configured`, `console_url_configured` and the known `sources` |
| `POST /channels` | 201. A webhook needs `url`; an email channel needs `recipients`. 409 for a duplicate name, 422 for a refused URL, an unknown source, a bad address or a short signing secret |
| `PATCH /channels/{id}` | Omitted fields are unchanged. An empty string removes `signing_secret` or `authorization`. The kind can't change |
| `DELETE /channels/{id}` | 204. The channel's delivery history is removed with it |
| `POST /channels/{id}/test` | sends now; `{ok, detail, code}`. A failure is still a 200 |
| `GET /deliveries` | `?channel_id=&status=&limit=&offset=` |

Audit actions:

- `NOTIFICATION_CHANNEL_CREATED`, `NOTIFICATION_CHANNEL_UPDATED`,
  `NOTIFICATION_CHANNEL_DELETED`: with where alerts go, never a secret.
- `NOTIFICATION_CHANNEL_TESTED`: with the outcome. A failed test is recorded with result
  `FAILURE`.

## Not built

- **Digests and quiet hours.** Each event is its own message. During an incident with many
  alerts, a chat channel receives one message per alert. A minimum severity, or a source
  filter, keeps busy channels quiet.
- **Per-team routing.** Channels see every alert that matches their filter, whichever team
  owns the resource.
- **Paging services' native APIs**, such as PagerDuty's Events API. Use a JSON webhook to
  an internal relay instead.
- **HTML email** and **SMS**.
- **Prometheus rules.** These go through Grafana and are not routed through channels. See
  [observability.md](observability.md#alerts).
