/* Private AI Platform — Settings (E8).
 *
 * Loaded after admin.js and uses its helpers (api, esc, pill, empty, guard, alert, can,
 * sectionCard, tableHtml, ago, onModalSubmit, closeModal).
 *
 * Two kinds of row, never mixed up on screen:
 *  - runtime settings: editable by holders of settings.manage, applied without a restart,
 *    audited, and resettable to the deployment's value;
 *  - deployment settings: read-only, from .env / config.yaml, applied on restart. Secrets
 *    arrive from the API already redacted.
 */
'use strict';

const settingsState = { data: null, editing: null };

const SOURCE_PILL = {
  override: ['INFO', 'Override'],
  configured: ['IDLE', 'Configured'],
  default: ['IDLE', 'Default'],
};

function settingValueText(value) {
  if (value === null || value === undefined || value === '') return '<span class="text-secondary">not set</span>';
  if (Array.isArray(value)) {
    return value.length
      ? value.map((v) => `<code>${esc(String(v))}</code>`).join(' ')
      : '<span class="text-secondary">none</span>';
  }
  if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
  if (typeof value === 'object') return `<code>${esc(JSON.stringify(value))}</code>`;
  return `<span class="mono">${esc(String(value))}</span>`;
}

function settingRow(item) {
  const [tone, label] = SOURCE_PILL[item.source] || ['IDLE', item.source];
  const manage = item.editable && can('settings.manage');
  const actions = manage
    ? `<button class="btn btn-sm btn-outline-primary" data-setting-edit="${esc(item.key)}">Change</button>
       ${item.source === 'override'
         ? `<button class="btn btn-sm btn-outline-secondary ms-1" data-setting-reset="${esc(item.key)}">Reset</button>`
         : ''}`
    : '';
  const deployed = item.editable && item.source === 'override'
    ? `<div class="small text-secondary">deployment value ${settingValueText(item.configured_value)}</div>` : '';
  const problem = item.problem
    ? `<div class="small text-danger mt-1">Stored override ignored: ${esc(item.problem)}</div>` : '';
  return `<tr data-setting-row="${esc(`${item.key} ${item.label} ${item.env_var}`.toLowerCase())}">
    <td>
      <div class="fw-medium">${esc(item.label)}</div>
      ${item.description ? `<div class="small text-secondary">${esc(item.description)}</div>` : ''}
      <div class="small mono text-secondary">${esc(item.env_var)}</div>
      ${problem}
    </td>
    <td>${settingValueText(item.value)}${deployed}
      ${item.updated_at ? `<div class="small text-secondary">changed ${esc(ago(item.updated_at))}</div>` : ''}</td>
    <td>${pill(tone, label)}</td>
    <td class="small text-secondary">${esc(item.applies)}</td>
    <td class="text-end text-nowrap">${actions}</td>
  </tr>`;
}

async function renderSettings({ force = false } = {}) {
  // Settings change rarely and the page holds a filter box: poll only on navigation.
  if (!force && settingsState.data) return;
  const body = document.getElementById('settings-body');
  if (!can('settings.view')) {
    body.innerHTML = empty('You need the settings.view permission to see platform settings.');
    return;
  }
  const data = await api('/settings');
  settingsState.data = data;

  // Categories holding runtime settings first: they are what most visits are for.
  const editableIn = (id) => data.items.some((i) => i.category === id && i.editable);
  const categories = [...data.categories].sort((a, b) => Number(editableIn(b.id)) - Number(editableIn(a.id)));
  const sections = categories.map((category) => {
    const items = data.items.filter((i) => i.category === category.id);
    if (!items.length) return '';
    // Runtime first: they are what someone opening this page came to change.
    items.sort((a, b) => Number(b.editable) - Number(a.editable));
    const runtime = items.filter((i) => i.editable).length;
    const subtitle = runtime
      ? `${runtime} runtime setting(s), applied without a restart${items.length > runtime ? '; the rest apply on restart' : ''}.`
      : 'Set in the deployment environment; changes apply on restart.';
    return `<div data-setting-section>${sectionCard(category.label, subtitle, tableHtml(
      `<th>Setting</th><th style="width:22%">Value</th><th style="width:8rem">Source</th>
       <th style="width:12rem">Applies</th><th style="width:9rem"></th>`,
      items.map(settingRow).join(''),
    ))}</div>`;
  }).join('');

  const overrides = data.items.filter((i) => i.source === 'override').length;
  body.innerHTML = `
    <div class="alert alert-info small py-2 d-flex gap-2 align-items-start">
      ${icon('i-info')}
      <div>${overrides
        ? `<strong>${overrides} runtime override(s) in effect.</strong> `
        : 'No runtime overrides: every value comes from the deployment. '}
        A change reaches every API process within ${data.propagation_seconds}s. The full
        effective configuration, redacted, is on the
        <a href="#diagnostics">Diagnostics</a> page.</div>
    </div>
    ${sections}
    <div id="settings-no-match" class="d-none">${empty('No setting matches that filter.')}</div>`;
  applySettingsFilter();

  body.querySelectorAll('[data-setting-edit]').forEach((b) =>
    b.addEventListener('click', () => openSettingModal(b.dataset.settingEdit)));
  body.querySelectorAll('[data-setting-reset]').forEach((b) =>
    b.addEventListener('click', () => guard(b, () => resetSetting(b.dataset.settingReset))));
}

function applySettingsFilter() {
  const q = document.getElementById('settings-filter').value.trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll('#settings-body [data-setting-section]').forEach((section) => {
    let visible = 0;
    section.querySelectorAll('[data-setting-row]').forEach((row) => {
      const match = !q || row.dataset.settingRow.includes(q);
      row.classList.toggle('d-none', !match);
      if (match) visible += 1;
    });
    section.classList.toggle('d-none', !visible);
    shown += visible;
  });
  document.getElementById('settings-no-match')?.classList.toggle('d-none', shown > 0);
}

function settingInput(item) {
  const value = item.value;
  if (item.type === 'bool') {
    return `<select class="form-select" id="setting-value">
      <option value="true"${value ? ' selected' : ''}>Enabled</option>
      <option value="false"${value ? '' : ' selected'}>Disabled</option></select>`;
  }
  if (item.type === 'int' || item.type === 'float') {
    return `<input class="form-control" id="setting-value" type="number" required
      step="${item.type === 'int' ? '1' : 'any'}" value="${esc(String(value ?? ''))}">`;
  }
  return `<input class="form-control font-monospace" id="setting-value" required
    value="${esc(String(value ?? ''))}" spellcheck="false" autocomplete="off">`;
}

function readSettingInput(item) {
  const raw = document.getElementById('setting-value').value.trim();
  if (item.type === 'bool') return raw === 'true';
  if (item.type === 'int' || item.type === 'float') {
    if (raw === '' || Number.isNaN(Number(raw))) throw new Error('Enter a number.');
    return Number(raw);
  }
  return raw;
}

function openSettingModal(key) {
  const item = settingsState.data.items.find((i) => i.key === key);
  if (!item) return;
  settingsState.editing = item;
  document.getElementById('setting-title').textContent = item.label;
  document.getElementById('setting-description').textContent = item.description || '';
  document.getElementById('setting-facts').innerHTML = `
    <dt>Current</dt><dd>${settingValueText(item.value)}</dd>
    <dt>Deployment value</dt><dd>${settingValueText(item.configured_value)}</dd>
    <dt>Built-in default</dt><dd>${settingValueText(item.default_value)}</dd>
    <dt>Takes effect</dt><dd>${esc(item.applies)}</dd>`;
  document.getElementById('setting-input').innerHTML = settingInput(item);
  document.getElementById('setting-reason').value = '';
  document.getElementById('setting-error').classList.add('d-none');
  bootstrap.Modal.getOrCreateInstance(document.getElementById('setting-modal')).show();
}

async function resetSetting(key) {
  const item = settingsState.data.items.find((i) => i.key === key);
  // A native confirm is enough here: a reset only ever returns to the deployment's value.
  if (!window.confirm(`Return "${item?.label || key}" to its deployment value?`)) return;
  settingsState.data = await api(`/settings/${encodeURIComponent(key)}`, { method: 'DELETE' });
  alert(`${item?.label || key} reset to the deployment value.`, 'success');
  await renderSettings({ force: true });
}

onModalSubmit('setting-form', 'setting-error', async () => {
  const item = settingsState.editing;
  const value = readSettingInput(item);
  const reason = document.getElementById('setting-reason').value.trim() || null;
  settingsState.data = await api(`/settings/${encodeURIComponent(item.key)}`, {
    method: 'PUT', body: JSON.stringify({ value, reason }),
  });
  closeModal('setting-modal');
  alert(`${item.label} updated. ${item.applies === 'Immediately' ? 'In effect now.' : `Takes effect: ${item.applies.toLowerCase()}.`}`, 'success');
  await renderSettings({ force: true });
});

document.getElementById('settings-filter').addEventListener('input', applySettingsFilter);

RENDERERS.settings = renderSettings;
