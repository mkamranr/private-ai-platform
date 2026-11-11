/* Private AI Platform — workflows (E6).
 *
 * Loaded after admin.js and uses its helpers (api, esc, pill, empty, guard, alert, can,
 * teamChip, onModalSubmit...). Registers its page with RENDERERS and extends the
 * Approvals page with workflow steps.
 *
 * The builder edits a local copy of the draft (`wf.draft`). Nothing reaches the server
 * until "Save draft"; the 5-second poll refreshes the list and the runs table only, so it
 * never replaces edits in progress.
 */
'use strict';

const wf = {
  selected: null,     // workflow id
  detail: null,       // last WorkflowDetail from the server
  draft: null,        // the working copy being edited
  dirty: false,
  tab: 'builder',
  refs: null,         // models, bases, tools for pickers
  editing: null,      // node id in the node modal, or '' for a new node
  newType: null,
  nodeTypes: [],
};

Object.assign(PILL_TONE, {
  SUCCEEDED: 'ok', PUBLISHED: 'ok', APPROVED: 'ok',
  QUEUED: 'idle', DRAFT: 'idle', CANCELLED: 'idle', ARCHIVED: 'idle', EXPIRED: 'idle', SKIPPED: 'idle',
  WAITING: 'info', WAITING_FOR_APPROVAL: 'warn', PAUSED: 'warn', PENDING: 'warn', REJECTED: 'crit',
});

const WF_TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
const WF_OPERATORS = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains', 'not_contains', 'empty', 'not_empty'];
const WF_TIMED = new Set(['llm', 'retrieval', 'tool', 'http']);
const WF_TYPE_LABEL = {
  start: 'Start', llm: 'Model', retrieval: 'Retrieval', tool: 'Tool', http: 'HTTP',
  condition: 'Condition', transform: 'Transform', approval: 'Approval', delay: 'Delay', end: 'End',
};
const WF_BRANCHES = { condition: ['true', 'false'], approval: ['approved', 'rejected'] };

/* Per type: the config fields the node modal offers. `kind` picks the input. */
const WF_FIELDS = {
  start: [
    { k: 'required_inputs', label: 'Required inputs', kind: 'list',
      help: 'Comma-separated input keys. A run without them fails at the start.' },
  ],
  llm: [
    { k: 'model', label: 'Model or alias', kind: 'model' },
    { k: 'system', label: 'System prompt', kind: 'textarea', rows: 2 },
    { k: 'prompt', label: 'Prompt', kind: 'textarea', rows: 6,
      help: 'Refer to earlier values with {{ input.question }} or {{ nodes.search.context }}.' },
    { k: 'temperature', label: 'Temperature', kind: 'number', step: '0.1', half: true },
    { k: 'max_tokens', label: 'Max tokens', kind: 'number', half: true },
    { k: 'parse_json', label: 'Parse the reply as JSON (available as .json)', kind: 'check' },
  ],
  retrieval: [
    { k: 'knowledge_base_ids', label: 'Knowledge bases', kind: 'bases' },
    { k: 'query', label: 'Query', kind: 'text', placeholder: '{{ input.question }}' },
    { k: 'top_k', label: 'Passages (top k)', kind: 'number', half: true },
  ],
  tool: [
    { k: 'tool_id', label: 'Tool', kind: 'tool' },
    { k: 'arguments', label: 'Arguments (JSON)', kind: 'json', rows: 4,
      help: 'String values may be templates. HIGH and CRITICAL tools wait for approval.' },
  ],
  http: [
    { k: 'method', label: 'Method', kind: 'select', options: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'], half: true },
    { k: 'url', label: 'URL', kind: 'text', placeholder: 'http://erp.internal/api/orders' },
    { k: 'headers', label: 'Headers (JSON)', kind: 'json', rows: 3,
      help: 'Headers are stored in the definition, visible to anyone who can view it. Put credentials in a REST tool instead.' },
    { k: 'body', label: 'Body', kind: 'jsonish', rows: 4, help: 'JSON is sent as JSON; anything else as text.' },
  ],
  condition: [
    { k: 'left', label: 'Left', kind: 'text', placeholder: '{{ input.amount }}', half: true },
    { k: 'operator', label: 'Operator', kind: 'select', options: WF_OPERATORS, half: true },
    { k: 'right', label: 'Right', kind: 'jsonish', rows: 1, help: 'A number, text, or a template.' },
  ],
  transform: [
    { k: 'output', label: 'Output (JSON)', kind: 'json', rows: 6,
      help: 'Build a new object from earlier values, e.g. {"total": "{{ nodes.calc.json.total }}"}.' },
  ],
  approval: [
    { k: 'message', label: 'Message for the approver', kind: 'text', placeholder: 'Refund {{ input.amount }}?' },
    { k: 'details', label: 'Details shown (JSON)', kind: 'json', rows: 3 },
    { k: 'allow_self_approval', label: 'Whoever started the run may approve it', kind: 'check' },
  ],
  delay: [
    { k: 'seconds', label: 'Wait (seconds)', kind: 'number', half: true, help: 'Up to a week.' },
  ],
  end: [
    { k: 'output', label: 'Run output', kind: 'jsonish', rows: 5,
      help: 'Leave empty to return the last step\'s output.' },
  ],
};

/* -------------------------------------------------------------------------- */
/* List                                                                       */
/* -------------------------------------------------------------------------- */
async function renderWorkflows({ force = false } = {}) {
  const list = await api('/workflows');
  document.getElementById('wf-create-btn').classList.toggle('d-none', !can('workflow.edit'));
  document.getElementById('wf-body').innerHTML = list.length ? `
    <div class="card"><div class="table-responsive">
      <table class="table table-sm align-middle mb-0 wf-table">
        <thead><tr>
          <th>Workflow</th><th>Status</th><th>Version</th><th>Triggers</th>
          <th>Nodes</th><th>Last run</th>
        </tr></thead>
        <tbody>${list.map(workflowRow).join('')}</tbody>
      </table>
    </div></div>` : empty('No workflows yet. Create one from a template to get started.');

  document.querySelectorAll('[data-wf-open]').forEach((a) => {
    a.onclick = (event) => {
      event.preventDefault();
      if (wf.dirty && wf.selected !== a.dataset.wfOpen &&
          !confirm('Discard unsaved changes to the current workflow?')) return;
      selectWorkflow(a.dataset.wfOpen);
    };
  });

  // The selection is not dropped because one list lacks it: a write can reach the list a
  // moment after its response. A workflow that is really gone answers 404 below.
  if (!wf.selected) return;
  try {
    if (force && !wf.dirty) await loadDetail();
    else if (wf.tab === 'runs') await renderRunsTab();
  } catch (err) {
    if (!/No workflow/.test(err.message)) throw err;
    wf.selected = null; wf.detail = null; wf.draft = null; wf.dirty = false;
    document.getElementById('wf-detail').innerHTML = '';
  }
}

function workflowRow(w) {
  const triggers = [
    w.schedule_cron ? `<span class="chip" title="Next: ${esc(w.next_run_at || 'paused')}">${icon('i-clock')}${esc(w.schedule_cron)}</span>` : '',
    w.webhook_enabled ? `<span class="chip">${icon('i-external')}webhook</span>` : '',
  ].join(' ') || '<span class="text-secondary small">manual</span>';
  const last = w.last_run;
  return `<tr class="${w.id === wf.selected ? 'table-active' : ''}">
    <td><a href="#" class="fw-medium" data-wf-open="${esc(w.id)}">${esc(w.display_name)}</a>
      ${teamChip(w.team_id)}<div class="mono small text-secondary">${esc(w.name)}</div></td>
    <td>${pill(w.status)}</td>
    <td class="small">${w.published_version ? `v${esc(w.published_version)}` : '<span class="text-secondary">—</span>'}</td>
    <td class="small">${triggers}</td>
    <td class="small">${esc(w.node_count)}</td>
    <td class="small">${last ? `${pill(last.status)} <span class="text-secondary">${esc(ago(last.created_at))}</span>`
      : '<span class="text-secondary">never</span>'}</td>
  </tr>`;
}

async function selectWorkflow(id) {
  wf.selected = id;
  wf.tab = 'builder';
  wf.dirty = false;
  await loadDetail();
  document.querySelectorAll('.wf-table tr').forEach((tr) =>
    tr.classList.toggle('table-active', Boolean(tr.querySelector(`[data-wf-open="${id}"]`))));
  document.getElementById('wf-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadDetail() {
  wf.detail = await api(`/workflows/${wf.selected}`);
  wf.draft = structuredClone(wf.detail.draft || { nodes: [], edges: [] });
  wf.draft.nodes = wf.draft.nodes || [];
  wf.draft.edges = wf.draft.edges || [];
  wf.dirty = false;
  if (!wf.refs) await loadRefs();
  renderDetail();
}

async function loadRefs() {
  const safe = (p) => p.catch(() => null);
  const [models, aliases, bases, tools] = await Promise.all([
    safe(api('/models?limit=200')), safe(api('/model-aliases')),
    safe(api('/knowledge-bases')), safe(api('/tools')),
  ]);
  wf.refs = {
    models: [...new Set([
      ...((aliases || []).map((a) => a.alias)),
      ...(((models && (models.items || models)) || []).map((m) => m.name)),
    ])],
    bases: bases || [],
    tools: tools || [],
  };
}

/* -------------------------------------------------------------------------- */
/* Detail                                                                     */
/* -------------------------------------------------------------------------- */
function renderDetail() {
  const d = wf.detail;
  const edit = can('workflow.edit');
  const run = can('workflow.execute');
  const archived = d.status === 'ARCHIVED';
  document.getElementById('wf-detail').innerHTML = `
    <div class="card mb-3"><div class="card-body">
      <div class="d-flex flex-wrap align-items-start gap-2">
        <div class="me-auto min-w-0">
          <h5 class="mb-1">${esc(d.display_name)} ${teamChip(d.team_id)}</h5>
          <div class="small text-secondary"><span class="mono">${esc(d.name)}</span>
            ${d.description ? ` · ${esc(d.description)}` : ''}</div>
        </div>
        <div class="d-flex flex-wrap gap-2 align-items-center">
          ${pill(d.status)}
          <span class="chip">${d.published_version ? `published v${esc(d.published_version)}` : 'never published'}</span>
          <span id="wf-dirty-chip" class="chip chip-warn ${wf.dirty || d.unpublished_changes ? '' : 'd-none'}">
            ${wf.dirty ? 'unsaved changes' : 'draft differs from published'}</span>
        </div>
      </div>
      <div class="d-flex flex-wrap gap-2 mt-3">
        ${edit && !archived ? `<button class="btn btn-sm btn-primary" id="wf-publish"
          ${!wf.dirty && !d.unpublished_changes ? 'disabled title="Nothing changed since the published version"' : ''}>Publish</button>` : ''}
        ${edit && d.status === 'PUBLISHED' ? `<button class="btn btn-sm btn-outline-secondary" data-wf-status="PAUSED">Pause</button>` : ''}
        ${edit && d.status === 'PAUSED' ? `<button class="btn btn-sm btn-outline-success" data-wf-status="PUBLISHED">Resume</button>` : ''}
        ${edit && !archived ? `<button class="btn btn-sm btn-outline-secondary" data-wf-status="ARCHIVED">Archive</button>` : ''}
        ${edit ? `<button class="btn btn-sm btn-outline-danger ms-auto" id="wf-delete">Delete</button>` : ''}
      </div>
    </div></div>

    <ul class="nav nav-tabs mb-3" role="tablist">
      ${[['builder', 'Builder'], ['run', 'Run'], ['runs', 'Runs'], ['triggers', 'Triggers'], ['versions', 'Versions']]
        .filter(([t]) => t !== 'run' || run || edit)
        .map(([t, label]) => `<li class="nav-item"><button class="nav-link ${wf.tab === t ? 'active' : ''}"
           data-wf-tab="${t}" type="button">${label}</button></li>`).join('')}
    </ul>
    <div id="wf-tab"></div>`;

  document.querySelectorAll('[data-wf-tab]').forEach((b) => {
    b.onclick = () => { wf.tab = b.dataset.wfTab; renderDetail(); };
  });
  const publish = document.getElementById('wf-publish');
  if (publish) publish.onclick = () => guard(publish, publishWorkflow);
  document.querySelectorAll('[data-wf-status]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      const status = b.dataset.wfStatus;
      if (status === 'ARCHIVED' && !confirm('Archive this workflow? It stops running and can no longer be edited. This cannot be undone.')) return;
      await api(`/workflows/${wf.selected}/status`, { method: 'POST', body: JSON.stringify({ status }) });
      alert(`Workflow ${titleCase(status).toLowerCase()}.`, 'success');
      await refresh({ force: true });
    });
  });
  const del = document.getElementById('wf-delete');
  if (del) del.onclick = () => guard(del, async () => {
    if (!confirm(`Delete ${d.display_name} with its versions and run history? Archive keeps the history.`)) return;
    await api(`/workflows/${wf.selected}`, { method: 'DELETE' });
    wf.selected = null; wf.detail = null; wf.dirty = false;
    document.getElementById('wf-detail').innerHTML = '';
    alert('Workflow deleted.', 'success');
    await refresh({ force: true });
  });

  ({ builder: renderBuilder, run: renderRunTab, runs: renderRunsTab,
     triggers: renderTriggersTab, versions: renderVersionsTab })[wf.tab]();
}

async function publishWorkflow() {
  if (wf.dirty) {
    if (!confirm('Save your changes and publish them?')) return;
    await saveDraft();
  }
  const note = prompt('Version note (optional):', '');
  if (note === null) return;
  wf.detail = await api(`/workflows/${wf.selected}/publish`, {
    method: 'POST', body: JSON.stringify({ note: note || null }),
  });
  alert(`Published version ${wf.detail.published_version}.`, 'success');
  await refresh({ force: true });
}

function markDirty() {
  wf.dirty = true;
  const chip = document.getElementById('wf-dirty-chip');
  if (chip) { chip.textContent = 'unsaved changes'; chip.classList.remove('d-none'); }
  const save = document.getElementById('wf-save');
  if (save) save.disabled = false;
  const publish = document.getElementById('wf-publish');
  if (publish) { publish.disabled = false; publish.removeAttribute('title'); }
  const discard = document.getElementById('wf-discard');
  if (discard) discard.disabled = false;
}

async function saveDraft() {
  wf.detail = await api(`/workflows/${wf.selected}`, {
    method: 'PATCH', body: JSON.stringify({ draft: wf.draft }),
  });
  wf.dirty = false;
}

/* -------------------------------------------------------------------------- */
/* Builder                                                                    */
/* -------------------------------------------------------------------------- */
function renderBuilder() {
  const edit = can('workflow.edit') && wf.detail.status !== 'ARCHIVED';
  const nodes = wf.draft.nodes;
  document.getElementById('wf-tab').innerHTML = `
    <div id="wf-problems"></div>
    <div class="card mb-3">
      <div class="card-header py-2 small text-secondary d-flex align-items-center">
        Graph <span class="ms-auto">${Object.entries(WF_TYPE_LABEL).filter(([t]) =>
          nodes.some((n) => n.type === t)).map(([t, l]) =>
          `<span class="wf-legend wf-t-${t}"></span>${esc(l)}`).join(' ')}</span>
      </div>
      <div class="card-body wf-graph-wrap" id="wf-graph">${graphSvg(wf.draft)}</div>
    </div>

    <div class="row g-3">
      <div class="col-xl-7">
        <div class="card h-100">
          <div class="card-header py-2 d-flex align-items-center gap-2">
            <span class="small text-secondary">Nodes</span>
            ${edit ? `<select class="form-select form-select-sm ms-auto" id="wf-add-type" style="width:auto">
              ${Object.entries(WF_TYPE_LABEL).filter(([t]) => t !== 'start').map(([t, l]) =>
                `<option value="${t}">${esc(l)}</option>`).join('')}
            </select>
            <button class="btn btn-sm btn-outline-primary" id="wf-add-node">Add node</button>` : ''}
          </div>
          <div class="table-responsive">
            <table class="table table-sm align-middle mb-0">
              <tbody>${nodes.map((n) => nodeRow(n, edit)).join('') ||
                `<tr><td class="text-secondary small p-3">No nodes.</td></tr>`}</tbody>
            </table>
          </div>
        </div>
      </div>
      <div class="col-xl-5">
        <div class="card h-100">
          <div class="card-header py-2 d-flex align-items-center">
            <span class="small text-secondary">Connections</span>
            ${edit ? '<button class="btn btn-sm btn-outline-primary ms-auto" id="wf-add-edge">Add connection</button>' : ''}
          </div>
          <div class="table-responsive">
            <table class="table table-sm align-middle mb-0">
              <thead><tr><th class="small">From</th><th class="small">To</th><th class="small">When</th><th></th></tr></thead>
              <tbody>${wf.draft.edges.map((e, i) => edgeRow(e, i, edit)).join('') ||
                `<tr><td colspan="4" class="text-secondary small p-3">No connections.</td></tr>`}</tbody>
            </table>
          </div>
          <div class="card-body pt-2 small text-secondary">
            “When” follows a branch: <span class="mono">true/false</span> after a condition,
            <span class="mono">approved/rejected</span> after an approval, and
            <span class="mono">error</span> after any step, to handle its failure instead of failing the run.
          </div>
        </div>
      </div>
    </div>

    <details class="card mt-3" id="wf-json-card">
      <summary class="card-header py-2 small text-secondary">Definition (JSON)</summary>
      <div class="card-body">
        <textarea class="form-control mono small" id="wf-json" rows="14" ${edit ? '' : 'readonly'}
          >${esc(JSON.stringify(wf.draft, null, 2))}</textarea>
        ${edit ? '<button class="btn btn-sm btn-outline-secondary mt-2" id="wf-json-apply">Apply JSON</button>' : ''}
      </div>
    </details>

    ${edit ? `<div class="wf-savebar mt-3">
      <button class="btn btn-sm btn-primary" id="wf-save" ${wf.dirty ? '' : 'disabled'}>Save draft</button>
      <button class="btn btn-sm btn-outline-secondary" id="wf-discard" ${wf.dirty ? '' : 'disabled'}>Discard changes</button>
      <button class="btn btn-sm btn-outline-secondary" id="wf-validate">Check</button>
      <span class="small text-secondary ms-2">Drafts save in any state; publishing validates.</span>
    </div>` : ''}`;

  showProblems(wf.dirty ? null : wf.detail.problems);
  wireBuilder();
}

function nodeSummary(n) {
  const c = n.config || {};
  switch (n.type) {
    case 'start': return (c.required_inputs || []).length ? `needs ${c.required_inputs.join(', ')}` : 'any input';
    case 'llm': return c.model ? `model ${c.model}` : 'no model chosen';
    case 'retrieval': return `${(c.knowledge_base_ids || []).length} base(s), top ${c.top_k || 5}`;
    case 'tool': {
      const tool = (wf.refs?.tools || []).find((t) => t.id === c.tool_id);
      return tool ? `${tool.name} · ${tool.risk_level}` : (c.tool_id ? 'unknown tool' : 'no tool chosen');
    }
    case 'http': return `${c.method || 'GET'} ${c.url || '(no URL)'}`;
    case 'condition': return `${c.left ?? '?'} ${c.operator || '?'} ${c.right ?? ''}`;
    case 'approval': return c.message || 'no message';
    case 'delay': return `${c.seconds || 0}s`;
    case 'end': return c.output ? 'custom output' : 'last step\'s output';
    default: return '';
  }
}

function nodeRow(n, edit) {
  const retry = n.retry?.max_attempts > 1 ? `<span class="chip" title="Retries">×${esc(n.retry.max_attempts)}</span>` : '';
  return `<tr>
    <td style="width:7.5rem"><span class="wf-type wf-t-${esc(n.type)}">${esc(WF_TYPE_LABEL[n.type] || n.type)}</span></td>
    <td class="min-w-0"><div class="fw-medium">${esc(n.name || n.id)} ${retry}</div>
      <div class="small text-secondary text-truncate" style="max-width:24rem" title="${esc(nodeSummary(n))}">
        <span class="mono">${esc(n.id)}</span> · ${esc(nodeSummary(n))}</div></td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary" data-wf-edit="${esc(n.id)}">${edit ? 'Edit' : 'View'}</button>
      ${edit && n.type !== 'start' ? `<button class="btn btn-sm btn-outline-danger" data-wf-del="${esc(n.id)}" title="Remove">✕</button>` : ''}
    </td>
  </tr>`;
}

function edgeRow(e, i, edit) {
  const ids = wf.draft.nodes.map((n) => n.id);
  const from = wf.draft.nodes.find((n) => n.id === e.from);
  const whens = ['', ...(WF_BRANCHES[from?.type] || []), ...(from && from.type !== 'start' ? ['error'] : [])];
  const opts = (values, current, labels = {}) => values.map((v) =>
    `<option value="${esc(v)}" ${v === (current || '') ? 'selected' : ''}>${esc(labels[v] ?? v)}</option>`).join('');
  if (!edit) {
    return `<tr><td class="mono small">${esc(e.from)}</td><td class="mono small">${esc(e.to)}</td>
      <td class="small">${esc(e.when || '—')}</td><td></td></tr>`;
  }
  return `<tr>
    <td><select class="form-select form-select-sm mono" data-edge="${i}" data-edge-key="from">${opts(ids, e.from)}</select></td>
    <td><select class="form-select form-select-sm mono" data-edge="${i}" data-edge-key="to">${opts(ids, e.to)}</select></td>
    <td><select class="form-select form-select-sm" data-edge="${i}" data-edge-key="when">${opts(whens, e.when, { '': 'always' })}</select></td>
    <td class="text-end"><button class="btn btn-sm btn-outline-danger" data-edge-del="${i}" title="Remove">✕</button></td>
  </tr>`;
}

function wireBuilder() {
  const rerender = () => { renderBuilder(); markDirty(); };
  document.querySelectorAll('[data-wf-edit]').forEach((b) => { b.onclick = () => openNodeModal(b.dataset.wfEdit); });
  document.querySelectorAll('[data-wf-del]').forEach((b) => {
    b.onclick = () => {
      const id = b.dataset.wfDel;
      wf.draft.nodes = wf.draft.nodes.filter((n) => n.id !== id);
      wf.draft.edges = wf.draft.edges.filter((e) => e.from !== id && e.to !== id);
      rerender();
    };
  });
  const add = document.getElementById('wf-add-node');
  if (add) add.onclick = () => openNodeModal('', document.getElementById('wf-add-type').value);
  const addEdge = document.getElementById('wf-add-edge');
  if (addEdge) addEdge.onclick = () => {
    const ids = wf.draft.nodes.map((n) => n.id);
    if (ids.length < 2) { alert('Add another node first.'); return; }
    wf.draft.edges.push({ from: ids[ids.length - 2], to: ids[ids.length - 1] });
    rerender();
  };
  document.querySelectorAll('[data-edge]').forEach((s) => {
    s.onchange = () => {
      const edge = wf.draft.edges[Number(s.dataset.edge)];
      if (s.dataset.edgeKey === 'when') {
        if (s.value) edge.when = s.value; else delete edge.when;
      } else {
        edge[s.dataset.edgeKey] = s.value;
        if (s.dataset.edgeKey === 'from') delete edge.when;
      }
      rerender();
    };
  });
  document.querySelectorAll('[data-edge-del]').forEach((b) => {
    b.onclick = () => { wf.draft.edges.splice(Number(b.dataset.edgeDel), 1); rerender(); };
  });
  const apply = document.getElementById('wf-json-apply');
  if (apply) apply.onclick = () => {
    try {
      const parsed = JSON.parse(document.getElementById('wf-json').value);
      if (!Array.isArray(parsed.nodes) || !Array.isArray(parsed.edges || [])) throw new Error('Needs "nodes" and "edges" arrays.');
      parsed.edges = parsed.edges || [];
      wf.draft = parsed;
      rerender();
      document.getElementById('wf-json-card').open = true;
    } catch (err) {
      alert(`Not applied: ${err.message}`);
    }
  };
  const save = document.getElementById('wf-save');
  if (save) save.onclick = () => guard(save, async () => {
    await saveDraft();
    alert('Draft saved.', 'success');
    await refresh({ force: true });
  });
  const discard = document.getElementById('wf-discard');
  if (discard) discard.onclick = () => guard(discard, async () => {
    if (!confirm('Discard your unsaved changes?')) return;
    await loadDetail();
  });
  const check = document.getElementById('wf-validate');
  if (check) check.onclick = () => guard(check, async () => {
    const result = await api(`/workflows/${wf.selected}/validate`, {
      method: 'POST', body: JSON.stringify({ definition: wf.draft }),
    });
    showProblems(result.problems, result.valid ? result.order : null);
  });
}

function showProblems(problems, order) {
  const host = document.getElementById('wf-problems');
  if (!host) return;
  if (problems === null || problems === undefined) { host.innerHTML = ''; return; }
  if (!problems.length) {
    const same = !wf.dirty && wf.detail && !wf.detail.unpublished_changes;
    host.innerHTML = `<div class="alert alert-success py-2 small">${same
      ? `Valid. The draft matches published version ${esc(wf.detail.published_version)}.` : 'Valid and ready to publish.'}
      ${order ? `Runs in this order: <span class="mono">${order.map(esc).join(' → ')}</span>.` : ''}</div>`;
    return;
  }
  host.innerHTML = `<div class="alert alert-warning py-2 small mb-3">
    <div class="fw-medium mb-1">${problems.length} thing(s) to fix before publishing</div>
    <ul class="mb-0 ps-3">${problems.map((p) =>
      `<li>${p.node ? `<span class="mono">${esc(p.node)}</span>: ` : ''}${esc(p.message)}</li>`).join('')}</ul>
  </div>`;
}

/* -------------------------------------------------------------------------- */
/* Graph                                                                      */
/* -------------------------------------------------------------------------- */
/** Auto-layout: column = longest path from a source, rows in definition order. Saved
 *  positions are not used for layout, so a hand-edited JSON never draws overlapping. */
function graphLayout(def) {
  const nodes = def.nodes || [];
  const ids = nodes.map((n) => n.id);
  const edges = (def.edges || []).filter((e) => ids.includes(e.from) && ids.includes(e.to));
  const indeg = Object.fromEntries(ids.map((id) => [id, 0]));
  edges.forEach((e) => { indeg[e.to] += 1; });
  const level = Object.fromEntries(ids.map((id) => [id, 0]));
  const queue = ids.filter((id) => indeg[id] === 0);
  const seen = new Set();
  while (queue.length) {
    const id = queue.shift();
    seen.add(id);
    edges.filter((e) => e.from === id).forEach((e) => {
      level[e.to] = Math.max(level[e.to], level[id] + 1);
      indeg[e.to] -= 1;
      if (indeg[e.to] === 0) queue.push(e.to);
    });
  }
  // Nodes on a cycle never reach in-degree 0; park them in a final column.
  const maxLevel = Math.max(0, ...Object.values(level));
  ids.filter((id) => !seen.has(id)).forEach((id) => { level[id] = maxLevel + 1; });
  const columns = {};
  ids.forEach((id) => { (columns[level[id]] = columns[level[id]] || []).push(id); });
  const W = 168, H = 56, GX = 64, GY = 26, PAD = 16;
  const pos = {};
  Object.entries(columns).forEach(([col, list]) => {
    list.forEach((id, row) => { pos[id] = { x: PAD + Number(col) * (W + GX), y: PAD + row * (H + GY) }; });
  });
  const cols = Object.keys(columns).length;
  const rows = Math.max(1, ...Object.values(columns).map((l) => l.length));
  // A row of headroom below for edges routed under the boxes.
  return { pos, edges, W, H, GX, GY, width: PAD * 2 + cols * W + (cols - 1) * GX, height: PAD * 2 + rows * H + rows * GY };
}

/** status: optional {nodeId: 'succeeded'|'failed'|'waiting'|'skipped'|'running'} */
function graphSvg(def, status = null) {
  const nodes = def.nodes || [];
  if (!nodes.length) return '<div class="text-secondary small">Nothing to draw yet.</div>';
  const { pos, edges, W, H, GY, GX, width, height } = graphLayout(def);
  const lines = edges.map((e) => {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2;
    const back = x2 <= x1;
    const skips = x2 - x1 > W + GX + 1;
    let d;
    if (skips) {
      // Over a column: run below the lowest box in between, so the line is never hidden
      // behind a node it does not touch.
      const below = Math.max(a.y, b.y, ...Object.values(pos)
        .filter((p) => p.x > a.x && p.x < b.x).map((p) => p.y)) + H + GY / 2;
      d = `M${x1},${y1} C${x1 + 30},${y1} ${x1 + 10},${below} ${x1 + 40},${below} ` +
          `L${x2 - 40},${below} C${x2 - 10},${below} ${x2 - 30},${y2} ${x2},${y2}`;
    } else {
      const dx = back ? 60 : Math.max(24, (x2 - x1) / 2);
      d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    }
    const cls = e.when === 'error' ? 'wf-edge wf-edge-error'
      : (e.when === 'false' || e.when === 'rejected') ? 'wf-edge wf-edge-neg' : 'wf-edge';
    const ly = skips ? Math.max(a.y, b.y, ...Object.values(pos).filter((p) => p.x > a.x && p.x < b.x)
      .map((p) => p.y)) + H + GY / 2 - 5 : (y1 + y2) / 2 - 6;
    const label = e.when ? `<text class="wf-edge-label" x="${(x1 + x2) / 2}" y="${ly}"
      text-anchor="middle">${esc(e.when)}</text>` : '';
    return `<path class="${cls}" d="${d}" marker-end="url(#wf-arrow)"/>${label}`;
  }).join('');
  const boxes = nodes.map((n) => {
    const p = pos[n.id];
    const st = status ? (status[n.id] || 'pending') : '';
    const name = String(n.name || n.id);
    return `<g class="wf-node wf-t-${esc(n.type)} ${st ? `wf-st-${esc(st)}` : ''}" transform="translate(${p.x},${p.y})">
      <title>${esc(n.id)} (${esc(n.type)})${st ? ` — ${esc(st)}` : ''}</title>
      <rect width="${W}" height="${H}" rx="10"/>
      <rect class="wf-node-bar" width="5" height="${H}" rx="2.5"/>
      <text x="16" y="23" class="wf-node-type">${esc((WF_TYPE_LABEL[n.type] || n.type).toUpperCase())}</text>
      <text x="16" y="41" class="wf-node-name">${esc(name.length > 20 ? `${name.slice(0, 19)}…` : name)}</text>
      ${st ? `<circle class="wf-node-dot" cx="${W - 14}" cy="14" r="5"/>` : ''}
    </g>`;
  }).join('');
  return `<svg class="wf-graph" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img"
      aria-label="Workflow graph">
    <defs><marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" class="wf-arrow"/></marker></defs>
    ${lines}${boxes}
  </svg>`;
}

/* -------------------------------------------------------------------------- */
/* Node modal                                                                 */
/* -------------------------------------------------------------------------- */
function freshId(type) {
  const base = { llm: 'model', retrieval: 'search', http: 'call' }[type] || type;
  let id = base, n = 2;
  while (wf.draft.nodes.some((x) => x.id === id)) id = `${base}_${n++}`;
  return id;
}

function openNodeModal(id, newType = null) {
  const edit = can('workflow.edit') && wf.detail.status !== 'ARCHIVED';
  const isNew = id === '';
  const node = isNew
    ? { id: freshId(newType), type: newType, name: WF_TYPE_LABEL[newType], config: newType === 'http' ? { method: 'POST' } : {} }
    : wf.draft.nodes.find((n) => n.id === id);
  wf.editing = id;
  wf.newType = newType;
  const c = node.config || {};
  document.getElementById('wf-node-title').textContent =
    `${isNew ? 'Add' : edit ? 'Edit' : 'View'} ${WF_TYPE_LABEL[node.type] || node.type} node`;
  document.getElementById('wf-node-error').classList.add('d-none');
  const fields = (WF_FIELDS[node.type] || []).map((f) => fieldHtml(f, c[f.k])).join('');
  document.getElementById('wf-node-body').innerHTML = `
    <fieldset ${edit ? '' : 'disabled'}>
    <div class="row g-3">
      <div class="col-md-6">
        <label class="form-label small text-secondary" for="wfn-id">Id</label>
        <input class="form-control mono" id="wfn-id" value="${esc(node.id)}" required pattern="[a-z][a-z0-9_]{0,63}"
          ${node.type === 'start' ? 'readonly' : ''}>
        <div class="form-text small">Lowercase. Later steps refer to it as <span class="mono">{{ nodes.${esc(node.id)}.… }}</span>.</div>
      </div>
      <div class="col-md-6">
        <label class="form-label small text-secondary" for="wfn-name">Label</label>
        <input class="form-control" id="wfn-name" value="${esc(node.name || '')}">
      </div>
      ${fields}
      ${WF_TIMED.has(node.type) ? `
        <div class="col-12"><hr class="my-1"><div class="small text-secondary">Reliability</div></div>
        <div class="col-md-4">
          <label class="form-label small text-secondary" for="wfn-attempts">Attempts</label>
          <input class="form-control" type="number" min="1" max="5" id="wfn-attempts" value="${esc(node.retry?.max_attempts ?? 1)}">
        </div>
        <div class="col-md-4">
          <label class="form-label small text-secondary" for="wfn-backoff">Backoff (s)</label>
          <input class="form-control" type="number" min="0" max="60" step="0.5" id="wfn-backoff" value="${esc(node.retry?.backoff_seconds ?? 0)}">
          <div class="form-text small">Doubles after each failed attempt.</div>
        </div>
        <div class="col-md-4">
          <label class="form-label small text-secondary" for="wfn-timeout">Timeout (s)</label>
          <input class="form-control" type="number" min="1" max="600" id="wfn-timeout" value="${esc(node.timeout_seconds ?? '')}" placeholder="60">
        </div>` : ''}
    </div>
    </fieldset>
    <div class="small text-secondary mt-3">Templates: <span class="mono">{{ input.key }}</span>,
      <span class="mono">{{ nodes.&lt;id&gt;.&lt;field&gt; }}</span>, <span class="mono">{{ run.id }}</span>.
      A value that is only a template keeps its type (a list stays a list).</div>`;
  document.querySelector('#wf-node-form [type=submit]').classList.toggle('d-none', !edit);
  new bootstrap.Modal(document.getElementById('wf-node-modal')).show();
}

function fieldHtml(f, value) {
  const id = `wfn-c-${f.k}`;
  const col = f.half ? 'col-md-6' : 'col-12';
  const label = `<label class="form-label small text-secondary" for="${id}">${esc(f.label)}</label>`;
  const help = f.help ? `<div class="form-text small">${esc(f.help)}</div>` : '';
  const text = (v) => (v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v, null, 2));
  switch (f.kind) {
    case 'check':
      return `<div class="col-12"><div class="form-check">
        <input class="form-check-input" type="checkbox" id="${id}" ${value ? 'checked' : ''}>
        <label class="form-check-label small" for="${id}">${esc(f.label)}</label></div>${help}</div>`;
    case 'number':
      return `<div class="${col}">${label}<input class="form-control" type="number" step="${f.step || '1'}" id="${id}" value="${esc(value ?? '')}">${help}</div>`;
    case 'select':
      return `<div class="${col}">${label}<select class="form-select" id="${id}">${f.options.map((o) =>
        `<option ${o === value ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>${help}</div>`;
    case 'list':
      return `<div class="${col}">${label}<input class="form-control mono" id="${id}" value="${esc((value || []).join(', '))}">${help}</div>`;
    case 'textarea':
      return `<div class="${col}">${label}<textarea class="form-control" rows="${f.rows || 3}" id="${id}">${esc(value || '')}</textarea>${help}</div>`;
    case 'json': case 'jsonish':
      return `<div class="${col}">${label}<textarea class="form-control mono small" rows="${f.rows || 3}" id="${id}">${esc(text(value))}</textarea>${help}</div>`;
    case 'model':
      return `<div class="${col}">${label}<input class="form-control mono" id="${id}" list="wfn-models" value="${esc(value || '')}" placeholder="an alias or model name">
        <datalist id="wfn-models">${(wf.refs?.models || []).map((m) => `<option value="${esc(m)}">`).join('')}</datalist>
        <div class="form-text small">Called through the gateway, as the person who started the run.</div></div>`;
    case 'bases': {
      const chosen = new Set(value || []);
      const bases = wf.refs?.bases || [];
      return `<div class="${col}">${label}<div class="wf-checklist" id="${id}">${bases.length ? bases.map((b) => `
        <label class="form-check"><input class="form-check-input" type="checkbox" value="${esc(b.id)}" ${chosen.has(b.id) ? 'checked' : ''}>
          <span class="form-check-label small">${esc(b.display_name)} <span class="text-secondary mono">${esc(b.name)}</span></span></label>`).join('')
        : '<div class="small text-secondary">No knowledge bases you can see.</div>'}</div>${help}</div>`;
    }
    case 'tool': {
      const tools = wf.refs?.tools || [];
      return `<div class="${col}">${label}<select class="form-select" id="${id}"><option value="">Choose a tool…</option>${tools.map((t) =>
        `<option value="${esc(t.id)}" ${t.id === value ? 'selected' : ''}>${esc(t.name)} · ${esc(t.type)} · ${esc(t.risk_level)}${t.enabled ? '' : ' (disabled)'}</option>`).join('')}</select>
        <div class="form-text small">The tool runs with the permissions of whoever started the run.</div></div>`;
    }
    default:
      return `<div class="${col}">${label}<input class="form-control" id="${id}" value="${esc(value ?? '')}" placeholder="${esc(f.placeholder || '')}">${help}</div>`;
  }
}

function readField(f) {
  const el = document.getElementById(`wfn-c-${f.k}`);
  switch (f.kind) {
    case 'check': return el.checked || undefined;
    case 'number': return el.value === '' ? undefined : Number(el.value);
    case 'list': return el.value.split(',').map((s) => s.trim()).filter(Boolean);
    case 'bases': return [...el.querySelectorAll('input:checked')].map((i) => i.value);
    case 'json': {
      if (!el.value.trim()) return undefined;
      try { return JSON.parse(el.value); } catch { throw new Error(`${f.label} is not valid JSON.`); }
    }
    case 'jsonish': {
      const raw = el.value.trim();
      if (!raw) return undefined;
      try { return JSON.parse(raw); } catch { return el.value; }
    }
    default: return el.value === '' ? undefined : el.value;
  }
}

document.getElementById('wf-node-form').onsubmit = (event) => {
  event.preventDefault();
  const error = document.getElementById('wf-node-error');
  try {
    const isNew = wf.editing === '';
    const old = isNew ? null : wf.draft.nodes.find((n) => n.id === wf.editing);
    const type = isNew ? wf.newType : old.type;
    const id = document.getElementById('wfn-id').value.trim();
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(id)) throw new Error('Id: lowercase letters, digits and underscores, starting with a letter.');
    if (wf.draft.nodes.some((n) => n.id === id && n !== old)) throw new Error(`Another node is already called ${id}.`);
    const config = {};
    (WF_FIELDS[type] || []).forEach((f) => {
      const v = readField(f);
      if (v !== undefined && !(Array.isArray(v) && !v.length && f.kind === 'list')) config[f.k] = v;
    });
    const node = { ...(old || {}), id, type, name: document.getElementById('wfn-name').value.trim() || id, config };
    if (WF_TIMED.has(type)) {
      const attempts = Number(document.getElementById('wfn-attempts').value || 1);
      const backoff = Number(document.getElementById('wfn-backoff').value || 0);
      const timeout = document.getElementById('wfn-timeout').value;
      if (attempts > 1 || backoff > 0) node.retry = { max_attempts: attempts, backoff_seconds: backoff };
      else delete node.retry;
      if (timeout) node.timeout_seconds = Number(timeout); else delete node.timeout_seconds;
    }
    if (isNew) {
      wf.draft.nodes.push(node);
      // Connect it after the last node that is not an end, which is what someone adding
      // steps one after another means; they can rewire it in the table.
      const prev = [...wf.draft.nodes].reverse().find((n) => n !== node && n.type !== 'end');
      if (prev && node.type !== 'start') wf.draft.edges.push({ from: prev.id, to: id });
    } else {
      Object.keys(old).forEach((k) => delete old[k]);
      Object.assign(old, node);
      if (wf.editing !== id) {
        wf.draft.edges.forEach((e) => {
          if (e.from === wf.editing) e.from = id;
          if (e.to === wf.editing) e.to = id;
        });
      }
    }
    closeModal('wf-node-modal');
    renderBuilder();
    markDirty();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('d-none');
  }
};

/* -------------------------------------------------------------------------- */
/* Run tab                                                                    */
/* -------------------------------------------------------------------------- */
function renderRunTab() {
  const d = wf.detail;
  const start = (d.draft.nodes || []).find((n) => n.type === 'start');
  const sample = Object.fromEntries(((start?.config || {}).required_inputs || []).map((k) => [k, '']));
  const canTest = can('workflow.edit');
  const canRun = can('workflow.execute') && d.status === 'PUBLISHED';
  document.getElementById('wf-tab').innerHTML = `
    <div class="card"><div class="card-body">
      <label class="form-label small text-secondary" for="wf-run-input">Input (JSON object)</label>
      <textarea class="form-control mono small" id="wf-run-input" rows="6">${esc(JSON.stringify(sample, null, 2))}</textarea>
      <div class="d-flex flex-wrap gap-2 mt-3 align-items-center">
        ${canRun ? `<button class="btn btn-sm btn-primary" id="wf-run-published">Run v${esc(d.published_version)}</button>` : ''}
        ${canTest ? `<button class="btn btn-sm btn-outline-primary" id="wf-run-test">Test the saved draft</button>` : ''}
        <span class="small text-secondary">${d.status === 'PUBLISHED' ? ''
          : `The workflow is ${esc(d.status.toLowerCase())}; only test runs are possible. `}
          The run executes now and opens when it finishes or pauses.</span>
      </div>
      ${wf.dirty ? '<div class="alert alert-warning py-2 small mt-3 mb-0">You have unsaved changes. A test run uses the saved draft.</div>' : ''}
    </div></div>`;
  const go = (test) => async () => {
    let input;
    try { input = JSON.parse(document.getElementById('wf-run-input').value || '{}'); } catch { throw new Error('The input is not valid JSON.'); }
    const run = await api(`/workflows/${wf.selected}/runs`, {
      method: 'POST', body: JSON.stringify({ input, test, trigger: 'manual' }),
    });
    showWorkflowRun(run);
    refresh();
  };
  const p = document.getElementById('wf-run-published');
  if (p) p.onclick = () => guard(p, go(false));
  const t = document.getElementById('wf-run-test');
  if (t) t.onclick = () => guard(t, go(true));
}

/* -------------------------------------------------------------------------- */
/* Runs                                                                       */
/* -------------------------------------------------------------------------- */
function duration(run) {
  if (!run.started_at) return '—';
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const ms = end - new Date(run.started_at);
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60000)}m`;
}

async function renderRunsTab() {
  const host = document.getElementById('wf-tab');
  if (!host || wf.tab !== 'runs') return;
  const runs = await api(`/workflows/${wf.selected}/runs?limit=50`);
  host.innerHTML = runs.length ? `
    <div class="card"><div class="table-responsive">
      <table class="table table-sm align-middle mb-0">
        <thead><tr><th>Started</th><th>Status</th><th>Trigger</th><th>Version</th><th>Steps</th><th>Duration</th><th></th></tr></thead>
        <tbody>${runs.map((r) => `<tr>
          <td class="small text-nowrap">${esc(new Date(r.created_at).toLocaleString())}</td>
          <td>${pill(r.status)}</td>
          <td class="small">${esc(r.trigger)}</td>
          <td class="small">${r.version ? `v${esc(r.version)}` : '<span class="text-secondary">draft</span>'}</td>
          <td class="small">${esc(r.step_count)}</td>
          <td class="small">${esc(duration(r))}</td>
          <td class="text-end"><button class="btn btn-sm btn-outline-secondary" data-wf-run="${esc(r.id)}">Trace</button></td>
        </tr>`).join('')}</tbody>
      </table>
    </div></div>` : empty('No runs yet. Start one from the Run tab.');
  host.querySelectorAll('[data-wf-run]').forEach((b) => {
    b.onclick = () => guard(b, async () => showWorkflowRun(await api(`/workflow-runs/${b.dataset.wfRun}`)));
  });
}

function stepStatus(run) {
  const status = {};
  (run.steps || []).forEach((s) => { status[s.node_id] = s.status.toLowerCase(); });
  if (WF_TERMINAL.has(run.status)) {
    (run.definition.nodes || []).forEach((n) => { if (!status[n.id]) status[n.id] = 'skipped'; });
  }
  return status;
}

const jsonBlock = (v) => `<pre class="logs wf-json">${esc(JSON.stringify(v, null, 2))}</pre>`;

function showWorkflowRun(run) {
  const workflowName = wf.detail?.id === run.workflow_id ? wf.detail.display_name : 'Workflow';
  document.getElementById('wf-run-title').innerHTML =
    `${esc(workflowName)} — run ${pill(run.status)}`;
  const pending = (run.approvals || []).filter((a) => a.status === 'PENDING');
  const body = document.getElementById('wf-run-body');
  body.innerHTML = `
    <div class="row g-3 mb-3">
      ${[['Trigger', run.trigger], ['Version', run.version ? `v${run.version}` : 'draft'],
         ['Steps', run.step_count], ['Duration', duration(run)]].map(([label, value]) => `
        <div class="col-6 col-lg-3"><div class="stat-card">
          <div class="stat-value" style="font-size:1.1rem">${esc(value)}</div>
          <div class="stat-label">${esc(label)}</div>
        </div></div>`).join('')}
    </div>
    ${run.error ? `<div class="alert alert-danger py-2 small">${esc(run.error)}</div>` : ''}
    ${run.status === 'WAITING' && run.resume_at ? `<div class="alert alert-info py-2 small">Waiting until
      ${esc(new Date(run.resume_at).toLocaleString())}; the worker continues it then.</div>` : ''}
    ${pending.map((a) => `<div class="alert alert-warning py-2 small d-flex flex-wrap gap-2 align-items-center">
      <div class="me-auto"><strong>Waiting for approval</strong> at <span class="mono">${esc(a.node_id)}</span>: ${esc(a.message)}</div>
      ${can('workflow.approve') ? `
        <button class="btn btn-sm btn-success" data-wf-decide="yes" data-id="${esc(a.id)}">Approve</button>
        <button class="btn btn-sm btn-outline-danger" data-wf-decide="no" data-id="${esc(a.id)}">Reject</button>` : ''}
    </div>`).join('')}
    <div class="card mb-3"><div class="card-body wf-graph-wrap">${graphSvg(run.definition, stepStatus(run))}</div></div>
    <div class="row g-3 mb-3">
      <div class="col-lg-6"><div class="stat-label mb-1">Input</div>${jsonBlock(run.input)}</div>
      <div class="col-lg-6"><div class="stat-label mb-1">Output</div>${run.output ? jsonBlock(run.output) : '<div class="small text-secondary">—</div>'}</div>
    </div>
    <div class="stat-label mb-2">Steps</div>
    <div class="wf-steps">${(run.steps || []).map((s) => `
      <details class="wf-step">
        <summary>
          <span class="text-secondary mono small">${String(s.sequence).padStart(2, '0')}</span>
          <span class="wf-type wf-t-${esc(s.node_type)}">${esc(WF_TYPE_LABEL[s.node_type] || s.node_type)}</span>
          <span class="mono small">${esc(s.node_id)}</span>
          ${pill(s.status)}
          ${s.branch ? `<span class="chip">${esc(s.branch)}</span>` : ''}
          ${s.attempts > 1 ? `<span class="chip">${esc(s.attempts)} attempts</span>` : ''}
          <span class="ms-auto small text-secondary">${s.duration_ms != null ? `${esc(s.duration_ms)}ms` : ''}</span>
        </summary>
        <div class="wf-step-body">
          ${s.error ? `<div class="alert alert-danger py-1 px-2 small mb-2">${esc(s.error)}</div>` : ''}
          <div class="row g-2">
            <div class="col-lg-6"><div class="small text-secondary">Input</div>${jsonBlock(s.input)}</div>
            <div class="col-lg-6"><div class="small text-secondary">Output</div>${jsonBlock(s.output)}</div>
          </div>
        </div>
      </details>`).join('') || '<div class="small text-secondary">No steps yet — the run is queued.</div>'}
    </div>
    ${(run.approvals || []).filter((a) => a.status !== 'PENDING').length ? `
      <div class="stat-label mt-3 mb-2">Decisions</div>
      <ul class="small mb-0">${run.approvals.filter((a) => a.status !== 'PENDING').map((a) =>
        `<li><span class="mono">${esc(a.node_id)}</span> ${pill(a.status)} ${a.decided_at ? esc(ago(a.decided_at)) : ''}
         ${a.reason ? `— ${esc(a.reason)}` : ''}</li>`).join('')}</ul>` : ''}
    ${!WF_TERMINAL.has(run.status) && can('workflow.execute') ? `
      <div class="mt-3 text-end"><button class="btn btn-sm btn-outline-danger" id="wf-run-cancel">Cancel run</button></div>` : ''}`;

  body.querySelectorAll('[data-wf-decide]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      const updated = await decideWorkflowApproval(b.dataset.id, b.dataset.wfDecide === 'yes');
      if (updated) { showWorkflowRun(updated); refresh(); }
    });
  });
  const cancel = document.getElementById('wf-run-cancel');
  if (cancel) cancel.onclick = () => guard(cancel, async () => {
    if (!confirm('Cancel this run? Pending approvals on it expire.')) return;
    showWorkflowRun(await api(`/workflow-runs/${run.id}/cancel`, { method: 'POST' }));
    refresh();
  });
  const modal = document.getElementById('wf-run-modal');
  bootstrap.Modal.getOrCreateInstance(modal).show();
}

async function decideWorkflowApproval(id, approved) {
  const reason = prompt(approved ? 'Reason (optional):' : 'Why are you rejecting it?', '');
  if (!approved && reason === null) return null;
  if (approved && reason === null) return null;
  return api(`/workflow-approvals/${id}/decide`, {
    method: 'POST', body: JSON.stringify({ approved, reason: reason || null }),
  });
}

/* -------------------------------------------------------------------------- */
/* Triggers                                                                   */
/* -------------------------------------------------------------------------- */
function renderTriggersTab() {
  const d = wf.detail;
  const edit = can('workflow.edit') && d.status !== 'ARCHIVED';
  document.getElementById('wf-tab').innerHTML = `
    <div class="row g-3">
      <div class="col-xl-6"><div class="card h-100"><div class="card-body">
        <h6>Schedule</h6>
        <p class="small text-secondary">Five-field cron, in UTC. Fires the published version;
          a paused workflow does not fire. Missed times while the platform was down fire once on return.</p>
        <fieldset ${edit ? '' : 'disabled'}>
          <label class="form-label small text-secondary" for="wf-cron">Cron</label>
          <input class="form-control mono" id="wf-cron" value="${esc(d.schedule_cron || '')}" placeholder="0 7 * * 1-5">
          <div class="form-text small">${d.next_run_at ? `Next run: ${esc(new Date(d.next_run_at).toLocaleString())} (your local time)`
            : d.schedule_cron ? 'Not scheduled while the workflow is not published.' : 'No schedule.'}</div>
          <label class="form-label small text-secondary mt-3" for="wf-cron-input">Input for scheduled runs (JSON)</label>
          <textarea class="form-control mono small" id="wf-cron-input" rows="4">${esc(JSON.stringify(d.schedule_input || {}, null, 2))}</textarea>
          <div class="mt-3 d-flex gap-2">
            <button class="btn btn-sm btn-primary" id="wf-cron-save">Save schedule</button>
            ${d.schedule_cron ? '<button class="btn btn-sm btn-outline-secondary" id="wf-cron-clear">Remove</button>' : ''}
          </div>
        </fieldset>
      </div></div></div>
      <div class="col-xl-6"><div class="card h-100"><div class="card-body">
        <h6>Webhook</h6>
        <p class="small text-secondary">Another system starts a run by POSTing a JSON body,
          which becomes the run's input. It runs with the permissions of whoever published
          the version. The token is shown once; rotating it stops the old one at once.</p>
        <div class="mb-3">${d.webhook_enabled ? '<span class="pill pill-ok">Enabled</span>' : '<span class="pill pill-idle">Disabled</span>'}</div>
        <div id="wf-hook-token"></div>
        ${edit ? `<div class="d-flex gap-2">
          <button class="btn btn-sm btn-primary" id="wf-hook-rotate">${d.webhook_enabled ? 'Rotate token' : 'Enable webhook'}</button>
          ${d.webhook_enabled ? '<button class="btn btn-sm btn-outline-danger" id="wf-hook-off">Disable</button>' : ''}
        </div>` : ''}
      </div></div></div>
    </div>`;

  const saveCron = async (cron) => {
    let input = {};
    try { input = JSON.parse(document.getElementById('wf-cron-input').value || '{}'); } catch { throw new Error('The scheduled input is not valid JSON.'); }
    wf.detail = await api(`/workflows/${wf.selected}`, {
      method: 'PATCH', body: JSON.stringify({ schedule_cron: cron, schedule_input: input }),
    });
    alert(cron ? 'Schedule saved.' : 'Schedule removed.', 'success');
    renderDetail();
    refresh();
  };
  const save = document.getElementById('wf-cron-save');
  if (save) save.onclick = () => guard(save, () => saveCron(document.getElementById('wf-cron').value.trim()));
  const clear = document.getElementById('wf-cron-clear');
  if (clear) clear.onclick = () => guard(clear, () => saveCron(''));
  const rotate = document.getElementById('wf-hook-rotate');
  if (rotate) rotate.onclick = () => guard(rotate, async () => {
    if (d.webhook_enabled && !confirm('Rotate the token? Callers using the current one start failing now.')) return;
    const hook = await api(`/workflows/${wf.selected}/webhook`, { method: 'POST' });
    wf.detail = await api(`/workflows/${wf.selected}`);
    renderDetail();
    document.getElementById('wf-hook-token').innerHTML = `
      <div class="alert alert-success small">
        <div class="fw-medium mb-1">Copy the token now — it will not be shown again.</div>
        <div class="mono text-break mb-2">${esc(hook.token)}</div>
        <pre class="logs mb-0">curl -X POST ${esc(hook.url)} \\
  -H "X-Workflow-Token: ${esc(hook.token)}" \\
  -H "Content-Type: application/json" \\
  -d '{"example": true}'</pre>
      </div>`;
  });
  const off = document.getElementById('wf-hook-off');
  if (off) off.onclick = () => guard(off, async () => {
    if (!confirm('Disable the webhook? Callers start failing now.')) return;
    await api(`/workflows/${wf.selected}/webhook`, { method: 'DELETE' });
    wf.detail = await api(`/workflows/${wf.selected}`);
    renderDetail();
  });
}

/* -------------------------------------------------------------------------- */
/* Versions                                                                   */
/* -------------------------------------------------------------------------- */
async function renderVersionsTab() {
  const host = document.getElementById('wf-tab');
  const versions = await api(`/workflows/${wf.selected}/versions`);
  const edit = can('workflow.edit') && wf.detail.status !== 'ARCHIVED';
  host.innerHTML = versions.length ? versions.map((v) => `
    <div class="card mb-2"><div class="card-body py-2">
      <div class="d-flex flex-wrap align-items-center gap-2">
        <span class="fw-medium">v${esc(v.version)}</span>
        ${v.version === wf.detail.published_version ? pill('PUBLISHED', 'current') : ''}
        <span class="small text-secondary">${esc(new Date(v.created_at).toLocaleString())}</span>
        <span class="small">${esc(v.note || '')}</span>
        <span class="ms-auto d-flex gap-2">
          <button class="btn btn-sm btn-outline-secondary" data-wf-ver-view="${esc(v.version)}">Graph</button>
          ${edit ? `<button class="btn btn-sm btn-outline-secondary" data-wf-ver-restore="${esc(v.version)}">Copy to draft</button>` : ''}
        </span>
      </div>
      <div class="wf-graph-wrap mt-2 d-none" id="wf-ver-${esc(v.version)}">${graphSvg(v.definition)}</div>
    </div></div>`).join('') : empty('Nothing published yet.');
  host.querySelectorAll('[data-wf-ver-view]').forEach((b) => {
    b.onclick = () => document.getElementById(`wf-ver-${b.dataset.wfVerView}`).classList.toggle('d-none');
  });
  host.querySelectorAll('[data-wf-ver-restore]').forEach((b) => {
    b.onclick = () => {
      if (wf.dirty && !confirm('Replace your unsaved changes with this version?')) return;
      const v = versions.find((x) => String(x.version) === b.dataset.wfVerRestore);
      wf.draft = structuredClone(v.definition);
      wf.tab = 'builder';
      renderDetail();
      markDirty();
      alert(`Version ${v.version} copied into the draft. Save and publish to make it current.`, 'success');
    };
  });
}

/* -------------------------------------------------------------------------- */
/* Create                                                                     */
/* -------------------------------------------------------------------------- */
onModalSubmit('wf-form', 'wf-error', async (form) => {
  const created = await api('/workflows', {
    method: 'POST',
    body: JSON.stringify({
      name: field('wf-name'),
      display_name: field('wf-display'),
      description: field('wf-desc') || null,
      template: form.querySelector('[name=wf-template]:checked').value,
      team_id: teamField('wf-team'),
    }),
  });
  closeModal('wf-modal');
  form.reset();
  wf.selected = created.id;
  wf.tab = 'builder';
  // Shown from the response rather than re-read, which could run ahead of the write.
  wf.detail = created;
  wf.draft = structuredClone(created.draft);
  wf.dirty = false;
  if (!wf.refs) await loadRefs();
  renderDetail();
  alert(`Created ${created.display_name}. ${created.problems.length ? 'Fill in the highlighted steps, then publish.' : ''}`, 'success');
  await refresh();
});

document.getElementById('wf-modal').addEventListener('show.bs.modal', () => fillTeamPickers());

/* -------------------------------------------------------------------------- */
/* Approvals page                                                             */
/* -------------------------------------------------------------------------- */
let agentPendingCount = 0;
let workflowPendingCount = 0;
const baseApprovalBadge = updateApprovalBadge;
// eslint-disable-next-line no-global-assign
updateApprovalBadge = (count) => { agentPendingCount = count; baseApprovalBadge(agentPendingCount + workflowPendingCount); };

async function renderWorkflowApprovals() {
  const host = document.getElementById('wf-approvals-body');
  let pending = [];
  try {
    pending = await api('/workflow-approvals/pending');
  } catch (err) {
    host.innerHTML = empty(`${err.message} Deciding workflow steps requires the workflow.approve permission.`);
    workflowPendingCount = 0;
    baseApprovalBadge(agentPendingCount);
    return;
  }
  workflowPendingCount = pending.length;
  baseApprovalBadge(agentPendingCount + workflowPendingCount);
  const mine = (a) => currentUser && a.started_by === currentUser.id && !a.allow_self_approval && !currentUser.is_superuser;
  host.innerHTML = pending.length ? `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr><th>Workflow</th><th>Step</th><th>Details</th><th>Waiting</th><th class="text-end">Decision</th></tr></thead>
        <tbody>${pending.map((a) => `<tr>
          <td><div class="fw-medium">${esc(a.workflow_display_name)}</div><div class="mono small text-secondary">${esc(a.workflow_name)} · ${esc(a.trigger)}</div></td>
          <td><div class="small">${esc(a.message)}</div><div class="mono small text-secondary">${esc(a.node_id)}</div></td>
          <td class="small text-secondary text-truncate" style="max-width:18rem" title="${esc(JSON.stringify(a.details))}">${esc(JSON.stringify(a.details))}</td>
          <td class="small text-secondary">${esc(ago(a.requested_at))}</td>
          <td class="text-end text-nowrap">
            <button class="btn btn-sm btn-outline-secondary me-1" data-wfa-trace="${esc(a.run_id)}">Trace</button>
            ${mine(a) ? '<span class="small text-secondary">You started this run</span>' : `
            <button class="btn btn-sm btn-outline-success me-1" data-wfa="yes" data-id="${esc(a.id)}">Approve</button>
            <button class="btn btn-sm btn-outline-danger" data-wfa="no" data-id="${esc(a.id)}">Reject</button>`}
          </td>
        </tr>`).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary">The run continues as soon as you decide. Someone other
      than whoever started the run has to decide, unless the step allows otherwise.</div>`
    : empty('No workflow steps are waiting.');
  host.querySelectorAll('[data-wfa]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      const run = await decideWorkflowApproval(b.dataset.id, b.dataset.wfa === 'yes');
      if (!run) return;
      alert(`Run is now ${titleCase(run.status).toLowerCase()}.`, 'success');
      await renderWorkflowApprovals();
    });
  });
  host.querySelectorAll('[data-wfa-trace]').forEach((b) => {
    b.onclick = () => guard(b, async () => showWorkflowRun(await api(`/workflow-runs/${b.dataset.wfaTrace}`)));
  });
}

RENDERERS.workflows = renderWorkflows;
RENDERERS.approvals = async (opts) => {
  await renderApprovals(opts);
  await renderWorkflowApprovals();
};
