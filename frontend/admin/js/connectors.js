/* Private AI Platform — Connector Catalogue (E10).
 *
 * Loaded after admin.js and uses its helpers (api, esc, pill, empty, guard, alert, can,
 * icon, kpiTile, ago).
 *
 * The catalogue never enables anything. Install registers a server and discovers its
 * tools (disabled, HIGH risk); "Apply suggested risk" only relabels tools nobody has
 * reviewed. Enabling a tool and granting it to an agent stay on the Tools and Agents pages.
 */
'use strict';

const connState = { data: null };

const CONN_STATE = {
  available: ['IDLE', 'Not installed'],
  registered: ['INFO', 'Registered'],
  installed: ['HEALTHY', 'Installed'],
  unreachable: ['CRITICAL', 'Not answering'],
  invalid: ['WARNING', 'Manifest problem'],
};
const CONN_NETWORK = {
  none: ['No network', 'Needs nothing beyond its own process and volumes'],
  internal: ['Internal network', 'Talks to a service on this site’s network'],
};

function connCard(c) {
  const [tone, label] = CONN_STATE[c.state] || ['UNKNOWN', c.state];
  const manage = can('mcp.manage');
  const [netLabel, netTitle] = CONN_NETWORK[c.network] || ['Unknown network', ''];
  const chips = [
    `<span class="chip" title="${esc(netTitle)}">${esc(netLabel)}</span>`,
    c.writes === true ? '<span class="chip chip-warn" title="At least one tool changes something">Can write</span>'
      : c.writes === false ? '<span class="chip">Read-only</span>' : '',
  ].join(' ');
  const category = (connState.data?.categories || []).find((x) => x.id === c.category)?.label;

  const t = c.tools;
  const tools = c.server
    ? `<div class="conn-tools small">
        <span><strong>${t.total}</strong> tool(s)</span>
        <span><strong>${t.enabled}</strong> enabled</span>
        <span class="${t.disabled ? 'text-warning-emphasis' : ''}"><strong>${t.disabled}</strong> awaiting review</span>
        ${c.server.last_discovered_at ? `<span class="text-secondary">discovered ${esc(ago(c.server.last_discovered_at))}</span>` : ''}
      </div>
      ${c.state === 'unreachable' && c.server.status_detail ? `<div class="small text-danger mt-1">${esc(c.server.status_detail)}</div>` : ''}`
    : '';

  const config = c.config.length
    ? `<details class="conn-config small mt-2"><summary>Configuration (${c.config.length} variable(s) in <code>.env</code>)</summary>
        <ul class="list-unstyled mb-0 mt-1">${c.config.map((v) => `<li><code>${esc(v.name)}</code>
          ${v.required ? '<span class="chip">required</span>' : ''}${v.secret ? ' <span class="chip">secret</span>' : ''}
          <span class="text-secondary">${esc(v.description)}</span></li>`).join('')}</ul></details>`
    : '';
  const problems = c.problems.length
    ? `<div class="alert alert-warning small py-2 mt-2 mb-0">Not installable until <code>${esc(c.file)}</code> is fixed:
        <ul class="mb-0">${c.problems.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div>` : '';

  const actions = [];
  if (manage && c.state !== 'invalid') {
    actions.push(`<button class="btn btn-sm ${c.server ? 'btn-outline-secondary' : 'btn-primary'}" data-conn-install="${esc(c.name)}">
      ${c.server ? 'Discover again' : 'Install'}</button>`);
  }
  if (manage && t.suggestions_pending) {
    actions.push(`<button class="btn btn-sm btn-outline-primary" data-conn-risk="${esc(c.name)}"
      title="Sets the manifest's suggested risk on tools nobody has reviewed. Enables nothing.">
      Apply suggested risk (${t.suggestions_pending})</button>`);
  }
  if (c.server && t.total) actions.push('<a class="btn btn-sm btn-outline-secondary" href="#tools">Review tools</a>');

  return `<div class="col-lg-6 col-xxl-4" data-conn-card data-conn-category="${esc(c.category || '')}"
      data-conn-text="${esc(`${c.name} ${c.display_name} ${c.description} ${c.package || ''}`.toLowerCase())}">
    <div class="card h-100"><div class="card-body d-flex flex-column">
      <div class="d-flex align-items-start gap-2 mb-1">
        <div class="me-auto min-w-0"><h6 class="mb-0">${esc(c.display_name)}</h6>
          <div class="small text-secondary mono">${esc(c.name)}</div></div>
        ${pill(tone, label)}
      </div>
      <p class="small mb-2">${esc(c.description)}</p>
      <div class="d-flex flex-wrap gap-1 mb-2">${category ? `<span class="chip">${esc(category)}</span>` : ''} ${chips}</div>
      ${c.package ? `<div class="small text-secondary text-break mb-2" title="${esc(c.package_kind)} package, version pinned">
        <span class="mono">${esc(c.package)}</span></div>` : ''}
      ${tools}${config}${problems}
      ${c.source ? `<div class="small text-secondary mt-2">Upstream: ${esc(c.source)}</div>` : ''}
      ${actions.length ? `<div class="d-flex flex-wrap gap-2 mt-auto pt-3">${actions.join('')}</div>` : ''}
    </div></div>
  </div>`;
}

function connKpis(items) {
  const installed = items.filter((c) => c.server).length;
  const unreviewed = items.reduce((n, c) => n + c.tools.disabled, 0);
  const enabled = items.reduce((n, c) => n + c.tools.enabled, 0);
  const down = items.filter((c) => c.state === 'unreachable').length;
  return `<div class="row g-3 mb-4">
    ${kpiTile({ label: 'Connectors', value: items.length, tone: 'blue', iconId: 'i-connectors', note: 'in the offline bundle' })}
    ${kpiTile({ label: 'Installed', value: installed, tone: 'violet', iconId: 'i-tools',
                note: down ? `${down} not answering` : 'all answering', valueClass: '' })}
    ${kpiTile({ label: 'Tools enabled', value: enabled, tone: 'green', iconId: 'i-check', note: 'granted case by case' })}
    ${kpiTile({ label: 'Awaiting review', value: unreviewed, tone: unreviewed ? 'amber' : 'green', iconId: 'i-shield',
                note: 'disabled until someone reads them' })}
  </div>`;
}

async function renderConnectors({ force = false } = {}) {
  const body = document.getElementById('connectors-body');
  if (!can('mcp.view')) {
    body.innerHTML = empty('You need the mcp.view permission to see the connector catalogue.');
    return;
  }
  if (!force && connState.data) return;
  const data = await api('/mcp/catalogue');
  connState.data = data;

  const select = document.getElementById('conn-category');
  const chosen = select.value;
  select.innerHTML = '<option value="">All categories</option>' + data.categories
    .map((c) => `<option value="${esc(c.id)}"${c.id === chosen ? ' selected' : ''}>${esc(c.label)}</option>`).join('');

  if (!data.items.length) {
    body.innerHTML = empty(`No connector manifests found in ${data.manifest_path}. They ship in the offline bundle under mcp/manifests/.`);
    return;
  }
  body.innerHTML = connKpis(data.items) + `
    <div class="alert alert-info small py-2 d-flex gap-2 align-items-start">${icon('i-info')}
      <div>A connector runs only after its image is vendored on a connected build machine
      (<code>make mcp-vendor</code>) and started (<code>make mcp</code>). The control plane
      cannot see containers, so "Not answering" can also mean "not started yet". Settings marked
      <em>secret</em> go in <code>.env</code>, never in a manifest.</div></div>`
    + `<div data-conn-group class="row g-3 mb-4">${data.items.map(connCard).join('')}</div>`
    + `<div id="conn-no-match" class="d-none">${empty('No connector matches that filter.')}</div>`;
  applyConnFilter();

  body.querySelectorAll('[data-conn-install]').forEach((b) => b.addEventListener('click', () => guard(b, async () => {
    const name = b.dataset.connInstall;
    const result = await api(`/mcp/catalogue/${encodeURIComponent(name)}/install`, { method: 'POST' });
    const detail = result.detail ? ` ${result.detail}` : '';
    alert(result.found
      ? `${result.connector.display_name}: ${result.found} tool(s) found, ${result.created} new.${detail}`
      : `${result.connector.display_name} is registered but did not answer.${detail}`,
      result.found ? 'success' : 'warning');
    await renderConnectors({ force: true });
  })));
  body.querySelectorAll('[data-conn-risk]').forEach((b) => b.addEventListener('click', () => guard(b, async () => {
    const name = b.dataset.connRisk;
    const result = await api(`/mcp/catalogue/${encodeURIComponent(name)}/apply-suggested-risk`, { method: 'POST' });
    alert(`Suggested risk set on ${result.changed} tool(s); none enabled.${result.skipped_reviewed
      ? ` ${result.skipped_reviewed} already-reviewed tool(s) left as they were.` : ''}`, 'success');
    await renderConnectors({ force: true });
  })));
}

function applyConnFilter() {
  const q = document.getElementById('conn-filter').value.trim().toLowerCase();
  const category = document.getElementById('conn-category').value;
  let shown = 0;
  document.querySelectorAll('#connectors-body [data-conn-group]').forEach((group) => {
    let visible = 0;
    group.querySelectorAll('[data-conn-card]').forEach((card) => {
      const match = (!q || card.dataset.connText.includes(q))
        && (!category || card.dataset.connCategory === category);
      card.classList.toggle('d-none', !match);
      if (match) visible += 1;
    });
    group.classList.toggle('d-none', !visible);
    shown += visible;
  });
  document.getElementById('conn-no-match')?.classList.toggle('d-none', shown > 0);
}

document.getElementById('conn-filter').addEventListener('input', applyConnFilter);
document.getElementById('conn-category').addEventListener('change', applyConnFilter);

RENDERERS.connectors = renderConnectors;
