/* Private AI Platform — Chargeback (E9) and cost budgets (E12).
 *
 * Loaded after admin.js and uses its helpers (api, esc, pill, empty, guard, alert, can,
 * icon, kpiTile, sectionCard, tableHtml, fmtInt, ago, onModalSubmit, closeModal, token, API).
 *
 * Amounts arrive as decimal strings and `null` means "not priced", which the page must
 * never show as 0: an unpriced model is a gap in the rate card, not a free one.
 */
'use strict';

const cbState = { key: null, report: null, rates: null, budgets: null };

const CB_GROUP_HEAD = {
  team: 'Team', api_key: 'API key', user: 'Platform user', end_user: 'End user', model: 'Model',
};
const RATE_STATUS = {
  in_force: ['HEALTHY', 'In force'],
  scheduled: ['INFO', 'Scheduled'],
  superseded: ['IDLE', 'Superseded'],
};

function isoDay(d) {
  return d.toISOString().slice(0, 10);
}

/** The selected period as UTC dates [start, end], both inclusive. */
function cbPeriod() {
  const choice = document.getElementById('cb-period').value;
  const today = new Date();
  const utc = (y, m, d) => new Date(Date.UTC(y, m, d));
  const y = today.getUTCFullYear(); const m = today.getUTCMonth();
  if (choice === 'last-month') return [isoDay(utc(y, m - 1, 1)), isoDay(utc(y, m, 0))];
  if (choice === '30d' || choice === '90d') {
    const days = choice === '30d' ? 29 : 89;
    return [isoDay(new Date(today.getTime() - days * 86400000)), isoDay(today)];
  }
  if (choice === 'custom') {
    return [document.getElementById('cb-start').value || isoDay(utc(y, m, 1)),
            document.getElementById('cb-end').value || isoDay(today)];
  }
  return [isoDay(utc(y, m, 1)), isoDay(today)];
}

function cbQuery() {
  const [start, end] = cbPeriod();
  const group = document.getElementById('cb-group').value;
  return new URLSearchParams({ group_by: group, start, end }).toString();
}

function money(value, currency) {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: n && Math.abs(n) < 1 ? 4 : 2,
    }).format(n);
  } catch {
    return `${n.toFixed(2)} ${currency}`;
  }
}

function moneyCell(value, currency, unpricedTitle = 'No rate applies') {
  const text = money(value, currency);
  return text === null
    ? `<span class="text-secondary" title="${esc(unpricedTitle)}">Not priced</span>`
    : `<span class="num">${esc(text)}</span>`;
}

function hoursText(h) {
  return h ? `${h.toLocaleString(undefined, { maximumFractionDigits: 2 })} h` : '—';
}

function cbRow(r, currency, group) {
  const flags = [];
  if (r.unpriced_requests) flags.push(`<span class="chip" title="Calls with no applicable rate">${fmtInt(r.unpriced_requests)} unpriced</span>`);
  if (r.partial_requests) flags.push(`<span class="chip" title="Streams the client abandoned: token counts are a lower bound">${fmtInt(r.partial_requests)} partial</span>`);
  if (group === 'end_user' && r.trusted === false) flags.push('<span class="chip" title="Named in the request body; nothing verified it">self-reported</span>');
  const detail = r.detail && !(group === 'end_user' && r.trusted !== null) ? r.detail : '';
  return `<tr>
    <td class="min-w-0">
      <div class="fw-medium text-truncate" style="max-width:20rem" title="${esc(r.label)}">${esc(r.label)}</div>
      ${detail ? `<div class="small text-secondary text-truncate" style="max-width:20rem">${esc(detail)}</div>` : ''}
      ${flags.length ? `<div class="d-flex flex-wrap gap-1 mt-1">${flags.join('')}</div>` : ''}
    </td>
    <td class="text-end">${fmtInt(r.requests)}</td>
    <td class="text-end">${fmtInt(r.prompt_tokens)}</td>
    <td class="text-end">${fmtInt(r.completion_tokens)}</td>
    <td class="text-end">${r.requests ? moneyCell(r.token_cost, currency) : '<span class="text-secondary">—</span>'}</td>
    <td class="text-end">${hoursText(r.gpu_hours)}</td>
    <td class="text-end">${r.gpu_hours ? moneyCell(r.gpu_cost, currency, 'No GPU-hour rate for this model') : '<span class="text-secondary">—</span>'}</td>
    <td class="text-end fw-medium">${moneyCell(r.total_cost, currency)}</td>
  </tr>`;
}

function cbReportCard(report) {
  const c = report.currency;
  const rows = [...report.rows, ...(report.idle ? [report.idle] : [])];
  if (!rows.length) {
    return sectionCard('Cost by ' + CB_GROUP_HEAD[report.group_by].toLowerCase(), null,
      `<div class="card-body pt-0">${empty('No gateway usage or GPU reservations in this period.')}</div>`);
  }
  const t = report.totals;
  const foot = `<tr class="fw-medium">
    <td>Total${report.truncated ? ` <span class="small text-secondary">(first ${fmtInt(report.rows.length)} rows shown; totals cover all)</span>` : ''}</td>
    <td class="text-end">${fmtInt(t.requests)}</td>
    <td class="text-end">${fmtInt(t.prompt_tokens)}</td>
    <td class="text-end">${fmtInt(t.completion_tokens)}</td>
    <td class="text-end">${moneyCell(t.token_cost, c)}</td>
    <td class="text-end">${hoursText(t.gpu_hours)}</td>
    <td class="text-end">${t.gpu_hours ? moneyCell(t.gpu_cost, c) : '—'}</td>
    <td class="text-end">${moneyCell(t.total_cost, c)}</td>
  </tr>`;
  const head = `<th>${esc(CB_GROUP_HEAD[report.group_by])}</th><th class="text-end">Requests</th>
    <th class="text-end">Prompt tokens</th><th class="text-end">Completion tokens</th>
    <th class="text-end">Token cost</th><th class="text-end">GPU time</th>
    <th class="text-end">GPU cost</th><th class="text-end">Total</th>`;
  return sectionCard(`Cost by ${CB_GROUP_HEAD[report.group_by].toLowerCase()}`,
    `${report.start} to ${report.end} (UTC). ${report.note}`,
    tableHtml(head, rows.map((r) => cbRow(r, c, report.group_by)).join('') + foot));
}

function cbRatesCard(rates) {
  const manage = can('cost.manage');
  const add = manage ? `<button class="btn btn-sm btn-primary" id="cb-add-rate">+ Add rate</button>` : '';
  const header = `<div class="card-body pb-0 d-flex align-items-start gap-2">
      <div class="me-auto"><h6 class="mb-1">Cost rates</h6>
        <p class="small text-secondary mb-2">Prices in ${esc(rates.currency)} (set on the Settings page).
          A model's own rate beats the default (<code>*</code>). Rates are never edited: add a new one
          with a later start to change a price.</p></div>${add}</div>`;
  if (!rates.items.length) {
    return `<div class="card mb-4">${header}<div class="card-body pt-0">${empty(
      'No rates yet, so every report shows usage as not priced. Add a default rate (*) to price every model, then per-model rates where they differ.')}</div></div>`;
  }
  const rows = rates.items.map((r) => {
    const [tone, label] = RATE_STATUS[r.status] || ['IDLE', r.status];
    return `<tr class="${r.status === 'superseded' ? 'text-secondary' : ''}">
      <td class="fw-medium">${r.model === '*' ? '<code>*</code> <span class="small text-secondary">default</span>' : esc(r.model)}
        ${r.note ? `<div class="small text-secondary">${esc(r.note)}</div>` : ''}</td>
      <td class="text-end num">${esc(money(r.prompt_per_million, rates.currency))}</td>
      <td class="text-end num">${esc(money(r.completion_per_million, rates.currency))}</td>
      <td class="text-end">${r.gpu_hour === null ? '<span class="text-secondary">not priced</span>' : `<span class="num">${esc(money(r.gpu_hour, rates.currency))}</span>`}</td>
      <td class="small">${esc(new Date(r.effective_from).toLocaleString())}
        ${r.superseded_at ? `<div class="text-secondary">until ${esc(new Date(r.superseded_at).toLocaleString())}</div>` : ''}</td>
      <td>${pill(tone, label)}</td>
      <td class="text-end">${manage ? `<button class="btn btn-sm btn-outline-danger" data-rate-delete="${esc(r.id)}">Remove</button>` : ''}</td>
    </tr>`;
  }).join('');
  return `<div class="card mb-4">${header}${tableHtml(
    `<th>Model</th><th class="text-end">1M prompt</th><th class="text-end">1M completion</th>
     <th class="text-end">GPU-hour</th><th>From</th><th>Status</th><th></th>`, rows)}</div>`;
}

const BUDGET_STATUS = {
  ok: ['HEALTHY', 'Within budget'],
  warning: ['WARNING', 'Near limit'],
  exceeded: ['CRITICAL', 'Exceeded'],
  unpriced: ['IDLE', 'Not priced'],
  disabled: ['IDLE', 'Off'],
};

/** Monthly cost budgets (E12): alerts only, never refusals. */
function cbBudgetsCard(budgets) {
  const manage = can('cost.manage');
  const add = manage ? '<button class="btn btn-sm btn-primary" id="cb-add-budget">+ Add budget</button>' : '';
  const header = `<div class="card-body pb-0 d-flex align-items-start gap-2">
      <div class="me-auto"><h6 class="mb-1">Monthly budgets</h6>
        <p class="small text-secondary mb-2">Estimated spend since ${esc(budgets.month_start)} (UTC) against each
          budget. A budget raises an alert at its warning share and at 100%. It never blocks traffic.</p></div>${add}</div>`;
  if (!budgets.items.length) {
    return `<div class="card mb-4">${header}<div class="card-body pt-0">${empty(
      'No budgets. Add one to be alerted before a team, an API key or the whole platform spends more than planned.')}</div></div>`;
  }
  const rows = budgets.items.map((b) => {
    const [tone, label] = BUDGET_STATUS[b.status] || ['IDLE', b.status];
    const pct = b.percent === null ? null : Math.min(b.percent, 100);
    const barTone = b.status === 'exceeded' ? 'bg-danger' : b.status === 'warning' ? 'bg-warning' : '';
    const bar = pct === null ? '' : `<div class="progress mt-1" style="height:6px" role="progressbar"
        aria-valuenow="${b.percent}" aria-valuemin="0" aria-valuemax="100" aria-label="Share of budget used">
        <div class="progress-bar ${barTone}" style="width:${pct}%"></div></div>`;
    const actions = manage ? `<div class="btn-group btn-group-sm">
        <button class="btn btn-outline-secondary" data-budget-edit="${esc(b.id)}">Edit</button>
        <button class="btn btn-outline-secondary" data-budget-toggle="${esc(b.id)}">${b.enabled ? 'Turn off' : 'Turn on'}</button>
        <button class="btn btn-outline-danger" data-budget-delete="${esc(b.id)}">Remove</button></div>` : '';
    return `<tr class="${b.enabled ? '' : 'text-secondary'}">
      <td class="fw-medium">${esc(b.subject_label)}
        ${b.description ? `<div class="small text-secondary">${esc(b.description)}</div>` : ''}</td>
      <td class="text-end num">${esc(money(b.monthly_amount, b.currency))}</td>
      <td class="text-end" style="min-width:9rem">${moneyCell(b.spend, b.currency, 'None of this usage has a rate')}
        ${b.spend_partial && b.spend !== null ? '<span class="chip" title="Some usage had no rate, so the real figure is higher">lower bound</span>' : ''}
        ${bar}</td>
      <td class="text-end">${b.percent === null ? '—' : `${b.percent}%`}</td>
      <td class="text-end">${b.warn_percent}%</td>
      <td>${pill(tone, label)}</td>
      <td class="text-end">${actions}</td>
    </tr>`;
  }).join('');
  return `<div class="card mb-4">${header}${tableHtml(
    `<th>Budget</th><th class="text-end">Per month</th><th class="text-end">Spent</th>
     <th class="text-end">Used</th><th class="text-end">Warn at</th><th>Status</th><th></th>`, rows)}</div>`;
}

async function fillBudgetSubjects(scope, selected = '') {
  const select = document.getElementById('budget-subject');
  select.disabled = scope === 'global';
  if (scope === 'global') {
    select.innerHTML = '<option value="">Everything, including idle GPU time</option>';
    return;
  }
  select.innerHTML = '<option value="">Loading…</option>';
  let options = [];
  try {
    if (scope === 'team') {
      options = (await api('/teams')).map((t) => [t.id, t.name]);
    } else {
      options = (await api('/api-keys')).filter((k) => !k.revoked_at).map((k) => [k.id, `${k.name} (${k.prefix}…)`]);
    }
  } catch { /* team.view or the key listing may be missing */ }
  select.innerHTML = options.length
    ? options.map(([id, name]) => `<option value="${esc(id)}"${id === selected ? ' selected' : ''}>${esc(name)}</option>`).join('')
    : '<option value="">Nothing you can see</option>';
}

async function openBudgetModal(budget = null) {
  document.getElementById('budget-form').reset();
  document.getElementById('budget-error').classList.add('d-none');
  document.querySelectorAll('.budget-currency').forEach((el) => { el.textContent = cbState.budgets?.currency || ''; });
  document.getElementById('budget-id').value = budget?.id || '';
  document.getElementById('budget-title').textContent = budget ? `Change budget: ${budget.subject_label}` : 'Add cost budget';
  document.getElementById('budget-submit').textContent = budget ? 'Save' : 'Add budget';
  document.getElementById('budget-subject-row').classList.toggle('d-none', Boolean(budget));
  if (budget) {
    document.getElementById('budget-amount').value = budget.monthly_amount;
    document.getElementById('budget-warn').value = budget.warn_percent;
    document.getElementById('budget-description').value = budget.description || '';
  } else {
    await fillBudgetSubjects('global');
  }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('budget-modal')).show();
}

document.getElementById('budget-scope').addEventListener('change', (e) =>
  fillBudgetSubjects(e.target.value).catch((err) => alert(err.message)));

onModalSubmit('budget-form', 'budget-error', async () => {
  const value = (id) => document.getElementById(id).value.trim();
  const id = value('budget-id');
  const common = {
    monthly_amount: value('budget-amount'),
    warn_percent: Number(value('budget-warn')),
    description: value('budget-description') || null,
  };
  if (id) {
    await api(`/chargeback/budgets/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(common) });
  } else {
    const scope = value('budget-scope');
    const subject = value('budget-subject');
    if (scope !== 'global' && !subject) throw new Error('Choose which team or API key the budget is for.');
    await api('/chargeback/budgets', {
      method: 'POST', body: JSON.stringify({ scope, subject: scope === 'global' ? null : subject, ...common }),
    });
  }
  closeModal('budget-modal');
  alert(id ? 'Budget changed.' : 'Budget added. Its alerts are checked every few minutes.', 'success');
  await renderChargeback({ force: true });
});

async function toggleBudget(id) {
  const budget = cbState.budgets.items.find((b) => b.id === id);
  await api(`/chargeback/budgets/${encodeURIComponent(id)}`, {
    method: 'PATCH', body: JSON.stringify({ enabled: !budget.enabled }),
  });
  await renderChargeback({ force: true });
}

async function deleteBudget(id) {
  const budget = cbState.budgets.items.find((b) => b.id === id);
  if (!window.confirm(`Remove the budget for ${budget?.subject_label}? Its open alert clears at the next check.`)) return;
  await api(`/chargeback/budgets/${encodeURIComponent(id)}`, { method: 'DELETE' });
  alert('Budget removed. The removal is recorded in the audit log.', 'success');
  await renderChargeback({ force: true });
}

function cbKpis(report) {
  const c = report.currency; const t = report.totals;
  const unpriced = report.unpriced_models.length;
  return `<div class="row g-3 mb-4">
    ${kpiTile({ label: 'Estimated total', value: money(t.total_cost, c) ?? 'Not priced', tone: 'blue', iconId: 'i-cost',
                note: `${report.start} to ${report.end}` })}
    ${kpiTile({ label: 'Token cost', value: money(t.token_cost, c) ?? 'Not priced', tone: 'violet', iconId: 'i-models',
                note: `${fmtInt(t.prompt_tokens + t.completion_tokens)} tokens` })}
    ${kpiTile({ label: 'GPU cost', value: t.gpu_hours ? (money(t.gpu_cost, c) ?? 'Not priced') : '—', tone: 'green', iconId: 'i-gpus',
                note: `${hoursText(t.gpu_hours)} held by model deployments` })}
    ${kpiTile({ label: 'Unpriced', value: fmtInt(t.unpriced_requests), tone: t.unpriced_requests ? 'amber' : 'green', iconId: 'i-alerts',
                note: unpriced ? `${unpriced} model(s) without a rate` : 'every call had a rate' })}
  </div>`;
}

async function renderChargeback({ force = false } = {}) {
  const body = document.getElementById('chargeback-body');
  document.getElementById('cb-export').classList.toggle('d-none', !can('cost.view'));
  if (!can('cost.view')) {
    body.innerHTML = empty('You need the cost.view permission to see chargeback reports.');
    return;
  }
  const key = cbQuery();
  // Reports cover whole days and rates change rarely: poll only on navigation or a change.
  if (!force && cbState.key === key && cbState.report) return;
  const [report, rates, budgets] = await Promise.all([
    api(`/chargeback/report?${key}`), api('/chargeback/rates'), api('/chargeback/budgets'),
  ]);
  Object.assign(cbState, { key, report, rates, budgets });

  let notice = '';
  if (!report.rates_defined) {
    notice = `<div class="alert alert-warning small py-2">${icon('i-info')} No cost rates are set, so the usage
      below is counted but not priced.${can('cost.manage') ? ' Add a rate under Cost rates.' : ''}</div>`;
  } else if (report.unpriced_models.length) {
    notice = `<div class="alert alert-warning small py-2">${icon('i-info')} No rate applies to
      ${report.unpriced_models.map((m) => `<code>${esc(m)}</code>`).join(', ')} for some or all of
      this period. Those calls are shown as <em>not priced</em>, not as free.</div>`;
  }
  const idle = report.idle
    ? `<div class="alert alert-secondary small py-2 d-flex gap-2 align-items-start mb-4">${icon('i-info')}
       <div><strong>Idle GPU capacity</strong> is GPU time held by models nobody called in the period
       (${esc(report.idle.detail)}). It is not shared out to callers.</div></div>` : '';
  body.innerHTML = cbKpis(report) + notice + cbReportCard(report) + idle + cbBudgetsCard(budgets)
    + cbRatesCard(rates);

  document.getElementById('cb-add-rate')?.addEventListener('click', openRateModal);
  document.getElementById('cb-add-budget')?.addEventListener('click', () =>
    openBudgetModal().catch((err) => alert(err.message)));
  body.querySelectorAll('[data-budget-edit]').forEach((b) => b.addEventListener('click', () =>
    openBudgetModal(cbState.budgets.items.find((x) => x.id === b.dataset.budgetEdit))));
  body.querySelectorAll('[data-budget-toggle]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => toggleBudget(b.dataset.budgetToggle))));
  body.querySelectorAll('[data-budget-delete]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => deleteBudget(b.dataset.budgetDelete))));
  body.querySelectorAll('[data-rate-delete]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => deleteRate(b.dataset.rateDelete))));
}

async function openRateModal() {
  document.getElementById('rate-form').reset();
  document.getElementById('rate-error').classList.add('d-none');
  document.querySelectorAll('.rate-currency').forEach((el) => { el.textContent = cbState.rates?.currency || ''; });
  const list = document.getElementById('rate-models');
  list.innerHTML = '<option value="*">';
  try {
    const models = await api('/models?limit=200');
    const names = (models.items || models).map((m) => m.name);
    list.innerHTML += names.map((n) => `<option value="${esc(n)}">`).join('');
  } catch { /* model.view may be missing; the field still takes a name */ }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('rate-modal')).show();
}

async function deleteRate(id) {
  const rate = cbState.rates.items.find((r) => r.id === id);
  if (!window.confirm(`Remove the ${rate?.model === '*' ? 'default' : rate?.model} rate? Reports for the period it covered fall back to the previous rate, or to not priced.`)) return;
  await api(`/chargeback/rates/${encodeURIComponent(id)}`, { method: 'DELETE' });
  alert('Rate removed. The removal is recorded in the audit log.', 'success');
  await renderChargeback({ force: true });
}

onModalSubmit('rate-form', 'rate-error', async () => {
  const value = (id) => document.getElementById(id).value.trim();
  const from = value('rate-from');
  const payload = {
    model: value('rate-model'),
    prompt_per_million: value('rate-prompt') || '0',
    completion_per_million: value('rate-completion') || '0',
    gpu_hour: value('rate-gpu') || null,
    effective_from: from ? new Date(from).toISOString() : null,
    note: value('rate-note') || null,
  };
  await api('/chargeback/rates', { method: 'POST', body: JSON.stringify(payload) });
  closeModal('rate-modal');
  alert(`Rate for ${payload.model === '*' ? 'every model without its own' : payload.model} added.`, 'success');
  await renderChargeback({ force: true });
});

function syncCustomPeriod() {
  const custom = document.getElementById('cb-period').value === 'custom';
  ['cb-start', 'cb-end'].forEach((id) => document.getElementById(id).classList.toggle('d-none', !custom));
  if (custom && !document.getElementById('cb-start').value) {
    const [start, end] = cbPeriod();
    document.getElementById('cb-start').value = start;
    document.getElementById('cb-end').value = end;
  }
}

['cb-group', 'cb-period', 'cb-start', 'cb-end'].forEach((id) =>
  document.getElementById(id).addEventListener('change', () => {
    syncCustomPeriod();
    renderChargeback({ force: true }).catch((err) => alert(err.message));
  }));

const cbExport = document.getElementById('cb-export');
cbExport.addEventListener('click', () => guard(cbExport, async () => {
  const response = await fetch(`${API}/chargeback/export?${cbQuery()}`, {
    method: 'POST', headers: { Authorization: `Bearer ${token.get()}` },
  });
  if (!response.ok) {
    let message = `Export failed (${response.status})`;
    try { message = (await response.json())?.error?.message || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  const blob = await response.blob();
  const name = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || 'chargeback.csv';
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement('a'), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  alert(`Downloaded ${name}. The export is recorded in the audit log.`, 'success');
}));

RENDERERS.chargeback = renderChargeback;
