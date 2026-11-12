/* Private AI Platform — alert notification channels (E13).
 *
 * Rendered under the alert list on the Alerts page for holders of notification.manage.
 * Webhook URLs and secrets are write-only: the API returns the host and whether a secret
 * is set, never the value, so the edit form leaves those fields blank to mean "unchanged".
 */
const notifyState = { data: null };

const NOTIFY_FORMAT_LABEL = { generic: 'JSON', chat: 'Chat text' };
const DELIVERY_PILL = {
  sent: ['HEALTHY', 'Sent'],
  failed: ['FAILED', 'Failed'],
  pending: ['WARNING', 'Queued'],
  cancelled: ['IDLE', 'Cancelled'],
};
const DELIVERY_EVENT_LABEL = { raised: 'Raised', escalated: 'Escalated', resolved: 'Resolved', test: 'Test' };

function channelHealth(c) {
  if (!c.enabled) return pill('IDLE', 'Off');
  if (c.consecutive_failures >= 3) return pill('CRITICAL', 'Failing');
  if (c.consecutive_failures > 0) return pill('WARNING', 'Last send failed');
  if (c.last_success_at) return pill('HEALTHY', `Sent ${ago(c.last_success_at)}`);
  return pill('IDLE', 'Not used yet');
}

function channelTarget(c) {
  if (c.kind === 'email') {
    const shown = c.recipients.slice(0, 2).map(esc).join(', ');
    const more = c.recipients.length > 2 ? ` +${c.recipients.length - 2}` : '';
    return `${shown}${more}`;
  }
  const secret = c.has_signing_secret ? ' <span class="chip" title="Requests are signed with HMAC-SHA256">signed</span>' : '';
  return `<code>${esc(c.url_display || '')}</code> <span class="chip">${esc(NOTIFY_FORMAT_LABEL[c.payload_format] || c.payload_format)}</span>${secret}`;
}

function channelRules(c) {
  const from = c.sources.length ? c.sources.map((s) => esc(titleCase(s))).join(', ') : 'every source';
  return `${esc(titleCase(c.min_severity))} and above · ${from}${c.notify_resolved ? ' · with resolutions' : ''}`;
}

function notifyChannelsCard(data) {
  const warnings = [];
  if (!data.smtp_configured && data.items.some((c) => c.kind === 'email')) {
    warnings.push('Email channels cannot send: no mail server is configured. Set <code>NOTIFICATIONS__SMTP_HOST</code> (and the other <code>NOTIFICATIONS__SMTP_*</code> settings) in <code>.env</code> and restart the backend.');
  }
  if (!data.console_url_configured && data.items.length) {
    warnings.push('Messages do not link back to this page. Set <code>NOTIFICATIONS__CONSOLE_URL</code> to the address people use for the console.');
  }
  const notices = warnings.map((w) => `<div class="alert alert-warning small py-2 mx-3 mb-2">${w}</div>`).join('');
  const header = `<div class="card-body pb-0 d-flex align-items-start gap-2">
      <div class="me-auto"><h6 class="mb-1">Notification channels</h6>
        <p class="small text-secondary mb-2">Where alerts are sent besides this page: chat or ticketing webhooks, and email.
          An alert is sent once when raised, again if it escalates, and when it is resolved. Repeats are not resent.</p></div>
      <button class="btn btn-sm btn-primary text-nowrap" id="notify-add">+ Add channel</button></div>`;
  if (!data.items.length) {
    return `<div class="card mb-4 mt-4">${header}<div class="card-body pt-0">${empty(
      'No channels yet. Add a Mattermost, Slack or Teams webhook, a SOC or ticketing endpoint, or an email list, so alerts reach someone who is not looking at the console.')}</div></div>`;
  }
  const rows = data.items.map((c) => `<tr class="${c.enabled ? '' : 'text-secondary'}">
      <td><div class="fw-medium">${esc(c.name)}</div>
        <div class="small text-secondary">${c.kind === 'email' ? 'Email' : 'Webhook'}${c.description ? ` · ${esc(c.description)}` : ''}</div></td>
      <td class="small">${channelTarget(c)}</td>
      <td class="small text-secondary">${channelRules(c)}</td>
      <td>${channelHealth(c)}${c.consecutive_failures && c.last_error
        ? `<div class="small text-danger mt-1" style="max-width:22rem">${esc(c.last_error)}</div>` : ''}</td>
      <td class="text-end">${c.pending || '—'}</td>
      <td class="text-end text-nowrap"><div class="btn-group btn-group-sm">
        <button class="btn btn-outline-primary" data-notify-test="${esc(c.id)}">Send test</button>
        <button class="btn btn-outline-secondary" data-notify-edit="${esc(c.id)}">Edit</button>
        <button class="btn btn-outline-secondary" data-notify-toggle="${esc(c.id)}">${c.enabled ? 'Turn off' : 'Turn on'}</button>
        <button class="btn btn-outline-danger" data-notify-delete="${esc(c.id)}">Remove</button></div></td>
    </tr>`).join('');
  return `<div class="card mb-4 mt-4">${header}${notices}${tableHtml(
    `<th>Channel</th><th>Sends to</th><th>What</th><th>Status</th><th class="text-end">Queued</th><th></th>`, rows)}</div>`;
}

function notifyDeliveriesCard(page) {
  if (!page.items.length) return '';
  const rows = page.items.map((d) => {
    const [tone, label] = DELIVERY_PILL[d.status] || ['IDLE', d.status];
    const retry = d.status === 'pending' && d.attempts
      ? `<div class="small text-secondary">retry ${esc(ago(d.next_attempt_at))}</div>` : '';
    return `<tr>
      <td class="small text-secondary text-nowrap" title="${esc(d.created_at)}">${esc(ago(d.created_at))}</td>
      <td class="small">${esc(d.channel_name)}</td>
      <td class="small">${esc(DELIVERY_EVENT_LABEL[d.event] || d.event)} ${d.event === 'test' ? '' : pill(d.severity)}</td>
      <td class="small">${esc(d.title)}</td>
      <td>${pill(tone, d.status === 'pending' && d.attempts ? 'Retrying' : label)}${retry}</td>
      <td class="text-end small">${d.attempts}</td>
      <td class="small text-danger" style="max-width:20rem">${esc(d.last_error || '')}</td>
    </tr>`;
  }).join('');
  return sectionCard('Recent notifications', 'The last messages sent, queued or given up on. Kept for 30 days.', tableHtml(
    `<th>When</th><th>Channel</th><th>Event</th><th>Alert</th><th>Status</th><th class="text-end">Attempts</th><th>Last error</th>`, rows));
}

/** Append the channels and recent deliveries to the Alerts page. */
async function renderNotifySection(body) {
  const [data, deliveries] = await Promise.all([
    api('/notifications/channels'),
    api('/notifications/deliveries?limit=15').catch(() => ({ items: [] })),
  ]);
  notifyState.data = data;
  const section = document.createElement('div');
  section.innerHTML = notifyChannelsCard(data) + notifyDeliveriesCard(deliveries);
  body.appendChild(section);

  section.querySelector('#notify-add')?.addEventListener('click', () => openNotifyModal());
  section.querySelectorAll('[data-notify-edit]').forEach((b) => b.addEventListener('click', () =>
    openNotifyModal(data.items.find((c) => c.id === b.dataset.notifyEdit))));
  section.querySelectorAll('[data-notify-test]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => testChannel(b.dataset.notifyTest))));
  section.querySelectorAll('[data-notify-toggle]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => toggleChannel(b.dataset.notifyToggle))));
  section.querySelectorAll('[data-notify-delete]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => deleteChannel(b.dataset.notifyDelete))));
}

function notifyKindFields(kind) {
  document.getElementById('notify-webhook-fields').classList.toggle('d-none', kind !== 'webhook');
  document.getElementById('notify-email-fields').classList.toggle('d-none', kind !== 'email');
  document.getElementById('notify-url').required = kind === 'webhook' && !document.getElementById('notify-id').value;
}

function openNotifyModal(channel = null) {
  const form = document.getElementById('notify-form');
  form.reset();
  document.getElementById('notify-error').classList.add('d-none');
  document.getElementById('notify-id').value = channel?.id || '';
  document.getElementById('notify-title').textContent = channel ? `Change channel: ${channel.name}` : 'Add notification channel';
  document.getElementById('notify-submit').textContent = channel ? 'Save' : 'Add channel';
  const kind = document.getElementById('notify-kind');
  kind.value = channel?.kind || 'webhook';
  kind.disabled = Boolean(channel);

  const sources = notifyState.data?.sources || [];
  document.getElementById('notify-sources').innerHTML = sources.map((s) => `
    <div class="form-check form-check-inline">
      <input class="form-check-input" type="checkbox" id="notify-src-${esc(s)}" value="${esc(s)}"
        ${channel?.sources.includes(s) ? 'checked' : ''}>
      <label class="form-check-label small" for="notify-src-${esc(s)}">${esc(titleCase(s))}</label>
    </div>`).join('');

  const urlHelp = document.getElementById('notify-url-help');
  urlHelp.textContent = channel?.url_display
    ? `Now ${channel.url_display}. Leave blank to keep the current address.`
    : 'Chat webhook URLs contain their credential, so the address is stored encrypted and never shown again.';
  for (const [name, has] of [['signing', channel?.has_signing_secret], ['auth', channel?.has_authorization]]) {
    document.getElementById(`notify-${name}-clear-row`).classList.toggle('d-none', !has);
    document.getElementById(`notify-${name}`).placeholder = has ? 'Set. Leave blank to keep it.' : '';
  }
  if (channel) {
    document.getElementById('notify-name').value = channel.name;
    document.getElementById('notify-description').value = channel.description || '';
    document.getElementById('notify-severity').value = channel.min_severity;
    document.getElementById('notify-resolved').checked = channel.notify_resolved;
    document.getElementById('notify-format').value = channel.payload_format;
    document.getElementById('notify-verify-tls').checked = channel.verify_tls;
    document.getElementById('notify-recipients').value = channel.recipients.join('\n');
  }
  notifyKindFields(kind.value);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('notify-modal')).show();
}

document.getElementById('notify-kind').addEventListener('change', (e) => notifyKindFields(e.target.value));

onModalSubmit('notify-form', 'notify-error', async () => {
  const value = (id) => document.getElementById(id).value.trim();
  const id = value('notify-id');
  const kind = document.getElementById('notify-kind').value;
  const body = {
    name: value('notify-name'),
    description: value('notify-description') || null,
    min_severity: value('notify-severity'),
    notify_resolved: document.getElementById('notify-resolved').checked,
    sources: [...document.querySelectorAll('#notify-sources input:checked')].map((i) => i.value),
  };
  if (!id) body.kind = kind;
  if (kind === 'webhook') {
    body.payload_format = value('notify-format');
    body.verify_tls = document.getElementById('notify-verify-tls').checked;
    if (value('notify-url')) body.url = value('notify-url');
    for (const [name, field, clear] of [['signing_secret', 'notify-signing', 'notify-signing-clear'],
      ['authorization', 'notify-auth', 'notify-auth-clear']]) {
      if (document.getElementById(clear).checked) body[name] = '';
      else if (value(field)) body[name] = value(field);
    }
  } else {
    body.recipients = value('notify-recipients').split(/[\s,;]+/).filter(Boolean);
  }
  const saved = id
    ? await api(`/notifications/channels/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) })
    : await api('/notifications/channels', { method: 'POST', body: JSON.stringify(body) });
  closeModal('notify-modal');
  alert(id ? `Channel ${saved.name} saved.`
    : `Channel ${saved.name} added. Use Send test to check it can be reached. Alerts raised from now on are sent to it.`, 'success');
  await refresh({ force: true });
});

async function testChannel(id) {
  const channel = notifyState.data.items.find((c) => c.id === id);
  const result = await api(`/notifications/channels/${encodeURIComponent(id)}/test`, { method: 'POST' });
  alert(result.ok ? `Test sent to ${channel.name}: ${result.detail}` : `Test to ${channel.name} failed: ${result.detail}`,
    result.ok ? 'success' : 'danger');
  await refresh({ force: true });
}

async function toggleChannel(id) {
  const channel = notifyState.data.items.find((c) => c.id === id);
  await api(`/notifications/channels/${encodeURIComponent(id)}`, {
    method: 'PATCH', body: JSON.stringify({ enabled: !channel.enabled }),
  });
  await refresh({ force: true });
}

async function deleteChannel(id) {
  const channel = notifyState.data.items.find((c) => c.id === id);
  if (!window.confirm(`Remove the channel ${channel.name}? Its delivery history is removed with it.`)) return;
  await api(`/notifications/channels/${encodeURIComponent(id)}`, { method: 'DELETE' });
  alert(`Channel ${channel.name} removed. The removal is recorded in the audit log.`, 'success');
  await refresh({ force: true });
}
