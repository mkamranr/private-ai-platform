# Alert notifications

In-app alerts only help if someone is looking at the console. A **notification
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
  `backup_schedule`, `notifications`, `auth`, `ops_runner` and `approvals` (reminders
  before an approval expires).
- **Team:** *Every team* (the default) or one team. A team's channel announces only
  alerts about that team. See [Team channels](#team-channels).

## Team channels

Point a team at its own chat or mailing list. Pick the team under *Alerts about* on the
channel form, or set `team_id` in the API. These alerts carry a team:

| Alert | Its team |
|---|---|
| `approvals`: a reminder about a waiting approval | the agent's or workflow's team |
| `quota_budget`, `cost_budget`: a team-scoped budget (by `team`) | that team |
| `deployment`: *Model deployment failed* | the model's team |

The rest are platform-wide (`team_id` null): backend health, backups, sign-in, the runner,
blocked models still running, a failing channel, and budgets for an API key, an end user or
the whole platform. A team's channel never announces these. A channel with no team
announces every alert, including each team's. The Alerts page shows a team chip
on alerts that have a team.

- An alert that is raised again takes the resource's current team. If an agent moves to
  another team, its next reminder goes to the new team's channel.
- When you change a channel's team, it starts from the change, like turning it back on. It
  does not replay the new team's open alerts.
- A team that has a channel cannot be deleted. The error lists `notification_channels`.
  Delete the channel or make it a platform channel first. Deleting a team leaves its old
  alerts platform-wide.
- Managing channels still needs `notification.manage`. A team owner cannot set up their
  team's channel themselves.

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
- **Quiet hours.** A channel can name a daily window, for example 22:00 to 07:00,
  when only **CRITICAL** alerts are sent. Anything else that comes due in the window is
  held, not dropped: it waits until the window ends and is then sent as its first attempt
  (held messages are not retries). A window whose end is before its start runs past
  midnight. Resolutions of a CRITICAL alert still go out; a test send is never held.
  - The window is read in the channel's time zone: `UTC`, a fixed offset such as `+04:00`,
    or a zone name such as `Asia/Dubai`. A name needs the server's time zone data, which a
    slim container may not have, so a name the server cannot resolve is refused when the
    channel is saved; use an offset instead. An offset does not follow daylight saving.
  - API: `quiet_start` and `quiet_end` (`HH:MM`, both or neither) and `quiet_timezone` on
    create and update; empty strings for both remove the window. Audited as `quiet_hours`.
  - **Digest.** When the window ends, a chat webhook or an email channel gets
    what it held as **one message** if there are two or more. The message is titled
    *[DIGEST] 5 notifications held during quiet hours for ops-chat* and has one line per
    alert, oldest first, for example `- [WARNING] Backend gpu-01 is not answering (raised
    2026-11-18 23:12 UTC)`. An alert that was raised and then resolved during the window
    gets two lines. The first 30 lines are listed and the rest are counted. A generic JSON
    webhook still gets one document per alert, because a program reading it expects
    that. A single held message goes out on its own. A digest is retried as a whole. Each
    delivery in it is marked sent and shares a `digest_id`, shown with `held_at` in
    `GET /deliveries`.

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

## PagerDuty

A webhook channel with the format **PagerDuty (Events API v2)** pages through PagerDuty
directly, without a relay:

1. In PagerDuty, add an **Events API v2** integration to the service that should be
   paged, and copy its *Integration Key* (32 characters).
2. On the Alerts page, add a webhook channel, choose *PagerDuty (Events API v2)*, and
   paste the key as the **Routing key**. The URL is filled in with PagerDuty's address,
   `https://events.pagerduty.com/v2/enqueue`. Change it if events go through a proxy or
   an on-premises relay.
3. Press *Send test*. It opens an incident and resolves it straight away, so nobody is
   left with an open page.

| Alert event | PagerDuty event |
|---|---|
| raised | `trigger`, with the severity as `critical`, `warning` or `info` |
| escalated | `trigger` again with the higher severity, which updates the same incident |
| resolved | `resolve` (if *Also say when an alert is resolved* is on) |

- **One incident per alert.** The dedup key is `aip:<alert fingerprint>`, so a repeat,
  an escalation and the resolution all land on the same incident.
- **What PagerDuty receives:** the summary (the alert title), source
  `private-ai-platform`, the alert source as group and class, the resource type as
  component, and the message, occurrences and ids as custom details. When
  `NOTIFICATIONS__CONSOLE_URL` is set, it also gets a link to the Alerts page.
- **The routing key is a secret.** It is stored encrypted, never shown again (the channel
  shows only that one is set), and never written to the audit log. A key that is not 32
  letters and digits is refused when you save.
- **No signing secret.** PagerDuty ignores signatures and authorises by routing key, so a
  signing secret is refused on a PagerDuty channel.
- **Quiet hours** still hold non-critical alerts, but a PagerDuty channel never gets a
  digest. Each alert is its own event, because PagerDuty deduplicates by key.
- **Network.** PagerDuty is on the internet, so the platform needs outbound access to
  `events.pagerduty.com`, or a relay. The outbound URL policy allows it, because it is not
  an AI service. A site with no outbound access can point the URL at an internal
  PagerDuty-compatible receiver.

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
| `NOTIFICATIONS__EMAIL_HTML` | true | send an HTML version as well as the plain text |
| `NOTIFICATIONS__EMAIL_LOGO_PATH` | unset | a logo embedded in each email (see [Branding](#branding)) |

- The subject is `[AI Platform] [CRITICAL] <title>`. The prefix can be changed under
  [Branding](#branding).
- **HTML.** Each message is `multipart/alternative`: the plain text, plus an HTML
  version with the same content. The HTML has a colour bar for the severity (green for
  resolved), the facts in a small table, and an *Open the Alerts page* button when
  `NOTIFICATIONS__CONSOLE_URL` is set. A digest is a list. The HTML is self-contained:
  inline styles only, with no fonts, scripts or remote resources (a logo, if set, is
  attached to the email). Nothing loads
  when it is opened, and it renders the same on an air-gapped network. Every alert value
  is escaped, so an alert's text cannot add markup. The approver reminder emails use
  the same format. If a mail gateway strips or quarantines HTML, set
  `NOTIFICATIONS__EMAIL_HTML=false` to send plain text only.

- A channel has up to 20 recipients; use a distribution list for more.
- If the server refuses some recipients but not all, the message counts as sent and the
  result says how many were refused.

### Branding

Every notification email uses one branded layout. The header shows your logo, or a
monogram of the brand's initials on the accent colour, next to the brand name and tagline.
Below it is a card with:

- a badge saying what happened (*Critical*, *Warning*, *Resolved*, *Digest*, *Approval
  waiting*, *Test*) and a coloured bar;
- the title and a sentence of context, for example *A critical alert was raised* or *This
  alert has been resolved. No action is needed.*;
- the alert's message and a short table of facts;
- a button in the accent colour to the console.

The footer holds your own text and says why the person received the email. A digest
lists its alerts with a count for each severity. An approver reminder links to the
**Approvals** page instead of Alerts, and says how to turn these emails off.

Set the branding on the **Settings** page under *Email branding*. These are runtime
settings, so they apply from the next email without a restart:

| Setting | Default | |
|---|---|---|
| Email brand name | `Private AI Platform` | |
| Email accent colour | `#4f46e5` | `#rrggbb`. Text on it turns dark or light to stay readable |
| Email tagline | empty | for example *IT Operations* |
| Email footer | empty | for example who to contact about an alert |
| Email subject prefix | `[AI Platform]` | mail filters often match on it; empty for none |

**Logo.** Set `NOTIFICATIONS__EMAIL_LOGO_PATH` to a PNG, JPEG or GIF of at most 200 KB
on the backend's filesystem, mounted into the container. It is shown 32 px high. The
logo is **attached to each email** as an inline image (`cid:`), never linked, so opening
an email loads nothing. A missing, oversized or non-image file is logged
(`email_logo_unreadable`, `email_logo_too_large`, `email_logo_not_an_image`) and the
email goes out with the monogram. The logo is a deployment setting, so it can't be
changed in the console, because the backend reads it from disk.

**Preview.** *Preview email* on the Alerts page (next to *+ Add channel*) shows a sample of
each kind, branded as currently set, with its subject. It is also available as
`GET /api/v1/notifications/email-preview?kind=raised|resolved|digest|approval|test`
(`notification.manage`). The preview needs no mail server and sends nothing.

The layout works in common mail clients: it uses tables for Outlook, adapts to narrow
screens, has dark-mode colours where the client supports them, and includes preview
text. It stays self-contained, with no web fonts, scripts or remote images, and every
value, including your branding text, is escaped.

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
| `PATCH /channels/{id}` | Omitted fields are unchanged. An empty string removes `signing_secret` or `authorization`. `team_id: null` makes it a platform channel. The kind can't change |
| `DELETE /channels/{id}` | 204. The channel's delivery history is removed with it |
| `POST /channels/{id}/test` | sends now; `{ok, detail, code}`. A failure is still a 200 |
| `GET /deliveries` | `?channel_id=&status=&limit=&offset=` |
| `GET /email-preview` | `?kind=raised`, `resolved`, `digest`, `approval` or `test`: `{kind, subject, text, html}`, branded as currently set; nothing is sent |

Audit actions:

- `NOTIFICATION_CHANNEL_CREATED`, `NOTIFICATION_CHANNEL_UPDATED`,
  `NOTIFICATION_CHANNEL_DELETED`: with where alerts go, never a secret.
- `NOTIFICATION_CHANNEL_TESTED`: with the outcome. A failed test is recorded with result
  `FAILURE`.

## Not built

- **Digests outside quiet hours.** What quiet hours held goes out as one
  digest. Otherwise each event is its own message, so during an incident with many
  alerts a chat channel receives one message per alert. A minimum severity or a source
  filter keeps busy channels quiet. There is no daily or hourly summary.
- **Team owners managing their own channels.** Team channels are set up by holders
  of `notification.manage`. An API key's budget alerts are not routed to the key's team.
- **Other paging services' native APIs** (Opsgenie, VictorOps). PagerDuty is built in.
  For the others, use a JSON webhook to an internal relay.
- **SMS.**
- **Prometheus rules.** These go through Grafana and are not routed through channels. See
  [observability.md](observability.md#alerts).
