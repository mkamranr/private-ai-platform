/* Private AI Platform — admin UI (M21).
 *
 * Plain ES modules-free JavaScript, no build step (§4.3). An air-gapped install must
 * be a file copy; a bundler would add a toolchain to vendor and reproduce on the
 * target for no benefit at this size.
 *
 * The token lives in sessionStorage rather than localStorage: it is cleared when the
 * tab closes, which is the right default for an administrative console on a shared
 * workstation.
 */
'use strict';

const API = '/api/v1';
const TOKEN_KEY = 'aip.token';
const POLL_MS = 5000;
/* Where docker-compose.dev.yml publishes the chat site. Production routes by name on
 * port 80 instead, so this is only consulted when the console is on a dev port. */
const CHAT_DEV_PORT = 8081;

let pollTimer = null;
let gpuChart = null;
let currentPage = 'dashboard';

/* -------------------------------------------------------------------------- */
/* API                                                                        */
/* -------------------------------------------------------------------------- */
const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (v) => sessionStorage.setItem(TOKEN_KEY, v),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

async function api(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) };
  const t = token.get();
  if (t) headers.Authorization = `Bearer ${t}`;

  const response = await fetch(`${API}${path}`, { ...options, headers });

  if (response.status === 401) {
    // A 401 from the login endpoint is a wrong password, not an expired session — there
    // was no session. Reporting it as one sends someone to sign in again with the same
    // credentials that just failed, and tells them nothing.
    const signingIn = path.startsWith('/auth/login');
    if (!signingIn) {
      // Expired or revoked. Bounce to login rather than leaving the UI in a state
      // where every request silently fails.
      token.clear();
      showLogin();
      throw new Error('Session expired. Sign in again.');
    }
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = body?.error?.message || body?.detail || message;
    } catch { /* non-JSON error body */ }
    throw new Error(message);
  }

  return response.status === 204 ? null : response.json();
}

/** Upload a file. Separate from api() because a multipart body must NOT carry an
 *  explicit Content-Type — the browser sets one with the boundary it generated, and
 *  overriding it produces a body the server cannot parse. */
async function apiUpload(path, file) {
  const body = new FormData();
  body.append('file', file);
  const headers = {};
  const t = token.get();
  if (t) headers.Authorization = `Bearer ${t}`;

  const response = await fetch(`${API}${path}`, { method: 'POST', headers, body });
  if (response.status === 401) {
    token.clear();
    showLogin();
    throw new Error('Session expired. Sign in again.');
  }
  if (!response.ok) {
    let message = `Upload failed (${response.status})`;
    try {
      const b = await response.json();
      message = b?.error?.message || b?.detail || message;
    } catch { /* non-JSON error body */ }
    throw new Error(message);
  }
  return response.json();
}

/* -------------------------------------------------------------------------- */
/* Rendering helpers                                                          */
/* -------------------------------------------------------------------------- */

/** Escape before interpolating into innerHTML.
 *  Container names, image tags and node labels are attacker-influenced in a real
 *  deployment — a container named `<img onerror=...>` must not execute here. */
function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

const dot = (status) => `<span class="status-dot status-${esc(status)}"></span>`;

function meter(percent) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  const band = value < 60 ? 'low' : value < 85 ? 'mid' : 'high';
  return `<div class="meter"><div class="meter-fill ${band}" style="width:${value}%"></div></div>`;
}

const mib = (v) => (v ? `${(v / 1024).toFixed(1)} GiB` : '—');

function ago(iso) {
  if (!iso) return 'never';
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function alert(message, kind = 'danger') {
  const slot = document.getElementById('alert-slot');
  slot.innerHTML =
    `<div class="alert alert-${kind} alert-dismissible py-2 small">${esc(message)}` +
    `<button class="btn-close btn-sm" data-bs-dismiss="alert"></button></div>`;
  if (kind === 'success') setTimeout(() => (slot.innerHTML = ''), 4000);
}

/* Every empty table on every screen renders through here, so the illustration is added
   once rather than at ~15 call sites. It is deliberately a *drawing of nothing* — an open
   tray — because the ambiguity worth removing is "nothing here yet" versus "this failed to
   load", which otherwise look identical and call for opposite responses.

   The text is still escaped; only the fixed markup around it is trusted. */
const EMPTY_ART =
  '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true">' +
  '<path d="M8 18 13 8h22l5 10" stroke="currentColor" stroke-width="2" ' +
  'stroke-linejoin="round" stroke-linecap="round"/>' +
  '<path d="M8 18h10l2.5 5h7L30 18h10v18a4 4 0 0 1-4 4H12a4 4 0 0 1-4-4V18Z" ' +
  'stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>' +
  '</svg>';

const empty = (text) =>
  `<div class="empty-state">${EMPTY_ART}<div class="empty-state-text">${esc(text)}</div></div>`;

/* -------------------------------------------------------------------------- */
/* Dashboard (M21)                                                            */
/* -------------------------------------------------------------------------- */

/* One request, not six. Six independent responses are six different instants, and a
 * dashboard assembled from them can show GPUs allocated to a deployment it also shows
 * as stopped. */
let usageChart = null;

async function renderDashboard() {
  const hours = Number(document.getElementById('dash-window').value) || 24;
  const data = await api(`/dashboard?window_hours=${hours}`);

  const cards = [];
  if (data.fleet) {
    cards.push(
      ['Nodes online', `${data.fleet.online} / ${data.fleet.total}`,
        data.fleet.offline ? `${data.fleet.offline} offline` : 'full fleet reporting',
        data.fleet.offline ? 'crit' : 'ok'],
    );
  }
  if (data.gpus) {
    const used = data.gpus.memory_total_mib
      ? Math.round((data.gpus.memory_used_mib / data.gpus.memory_total_mib) * 100) : 0;
    cards.push(
      ['GPUs free', `${data.gpus.free} / ${data.gpus.total}`,
        `${data.gpus.avg_utilization_percent.toFixed(0)}% busy · ${used}% memory`,
        data.gpus.free ? 'ok' : 'warn'],
    );
  }
  if (data.models) {
    cards.push(
      ['Models serving', data.models.running,
        `${data.models.available} available of ${data.models.registered} registered`,
        data.models.failed ? 'crit' : 'ok'],
    );
  }
  if (data.gateway) {
    cards.push(
      ['Gateway requests', data.gateway.requests.toLocaleString(),
        `${(data.gateway.prompt_tokens + data.gateway.completion_tokens).toLocaleString()} tokens · ` +
        `${data.gateway.avg_latency_ms.toFixed(0)}ms avg`, 'ok'],
    );
  }

  // Synthetic capacity must never read as real. Called out above the fold rather than
  // as a badge somewhere in a table.
  const syntheticWarning = data.fleet && data.fleet.synthetic
    ? `<div class="alert alert-warning py-2 small">
         <strong>${data.fleet.synthetic} of ${data.fleet.total} node(s) report synthetic GPU
         telemetry.</strong> The numbers below are fabricated for development and do not
         describe real hardware.
       </div>` : '';

  document.getElementById('dashboard-body').innerHTML = `
    ${syntheticWarning}
    <div class="row g-3 mb-4">
      ${cards.map(([label, value, note, band]) => `
        <div class="col-6 col-lg-3">
          <div class="stat-card">
            <div class="stat-value dash-${band}">${esc(value)}</div>
            <div class="stat-label">${esc(label)}</div>
            <div class="small text-secondary mt-1">${esc(note)}</div>
          </div>
        </div>`).join('')}
    </div>

    ${data.gateway ? `
      <div class="row g-3">
        <div class="col-lg-7">
          <div class="stat-card">
            <div class="stat-label mb-2">Gateway traffic</div>
            <canvas id="usage-chart" height="130"></canvas>
            <div id="usage-chart-empty" class="empty-state d-none">
              <div class="empty-state-text">
                No gateway traffic in this window. Deploy a model and call it, or widen the
                range above.
              </div>
            </div>
          </div>
        </div>
        <div class="col-lg-5">
          <div class="stat-card">
            <div class="stat-label mb-2">Busiest models</div>
            ${data.gateway.top_models.length ? `
              <table class="table table-sm align-middle mb-0">
                <tbody>${data.gateway.top_models.map((m) => `
                  <tr>
                    <td class="mono">${esc(m.model)}</td>
                    <td class="text-end small">${m.requests.toLocaleString()} req</td>
                    <td class="text-end small text-secondary">
                      ${(m.prompt_tokens + m.completion_tokens).toLocaleString()} tok</td>
                  </tr>`).join('')}
                </tbody>
              </table>` : '<div class="small text-secondary">No traffic in this window.</div>'}
          </div>
        </div>
      </div>` : ''}

    ${data.activity ? `
      <div class="stat-card mt-3">
        <div class="stat-label mb-2">Recent activity</div>
        ${data.activity.length ? `
          <table class="table table-sm align-middle mb-0">
            <tbody>${data.activity.map((a) => `
              <tr>
                <td class="small text-secondary" style="width:7rem">${esc(ago(a.at))}</td>
                <td class="small">${esc(a.username || 'system')}</td>
                <td class="small mono">${esc(a.action)}</td>
                <td class="small text-secondary">${esc(a.resource_type || '')}</td>
                <td class="text-end">${a.result === 'SUCCESS'
                  ? '<span class="badge text-bg-secondary">ok</span>'
                  : `<span class="badge text-bg-danger">${esc(a.result.toLowerCase())}</span>`}</td>
              </tr>`).join('')}
            </tbody>
          </table>` : '<div class="small text-secondary">Nothing recorded yet.</div>'}
      </div>` : ''}`;

  if (data.gateway) drawUsageChart(data.gateway.series);
}

function drawUsageChart(series) {
  const canvas = document.getElementById('usage-chart');
  if (!canvas) return;
  if (usageChart) usageChart.destroy();

  // An axis drawn over no data is worse than a sentence: it looks like a chart that
  // failed to load. Says which of the two it is instead.
  const idle = !series.length || series.every((p) => !p.requests && !p.tokens);
  const note = document.getElementById('usage-chart-empty');
  if (note) note.classList.toggle('d-none', !idle);
  canvas.classList.toggle('d-none', idle);
  if (idle) return;

  usageChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: series.map((p) => new Date(p.hour).toLocaleTimeString([], { hour: '2-digit' })),
      datasets: [
        // Capped width. The series is gap-filled server-side so a quiet platform still
        // spans its window, but a one-hour window would otherwise put a single category
        // across the whole plot — a solid block that reads as a rendering fault rather
        // than as "four requests, once".
        { label: 'Requests', data: series.map((p) => p.requests),
          backgroundColor: '#2ea043', maxBarThickness: 44, yAxisID: 'y' },
        // `pointRadius: 0` is right for a dense line and wrong for a sparse one: a line
        // needs two points to draw anything, so a single hour of traffic rendered as
        // nothing at all while the legend still advertised "Tokens".
        { label: 'Tokens', data: series.map((p) => p.tokens), type: 'line',
          borderColor: '#58a6ff', tension: .3, borderWidth: 2, yAxisID: 'y1',
          pointRadius: series.filter((p) => p.tokens > 0).length === 1 ? 3 : 0,
          pointBackgroundColor: '#58a6ff' },
      ],
    },
    options: {
      responsive: true,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: { beginAtZero: true, position: 'left', title: { display: true, text: 'requests' } },
        // Tokens outrun requests by two orders of magnitude, so one axis would flatten
        // the request bars into the baseline.
        y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false },
              title: { display: true, text: 'tokens' } },
      },
      plugins: { legend: { labels: { boxWidth: 12 } } },
    },
  });
}

/* -------------------------------------------------------------------------- */
/* Chat (M17)                                                                 */
/* -------------------------------------------------------------------------- */

/* Deliberately a pointer, not an embed. Open WebUI is a separate application with its
 * own session; putting it in an iframe would give it a second, confusingly different
 * login inside this one. */
async function renderChat() {
  let serving = [];
  try {
    serving = (await api('/model-aliases')).filter((a) => a.serving && a.enabled);
  } catch { /* the operator may lack model.view; the link still works */ }

  const chatUrl = `${location.protocol}//${chatHost()}`;
  document.getElementById('chat-body').innerHTML = `
    <div class="stat-card">
      <p class="mb-3">
        Chat runs on <a href="${esc(chatUrl)}" target="_blank" rel="noopener">${esc(chatUrl)}</a>
        — Open WebUI, consuming this platform's gateway and nothing else.
      </p>
      <p class="small text-secondary mb-3">
        It holds one API key for everyone, and forwards each signed-in user's identity as
        a signed assertion, so usage is attributed per person rather than to the frontend.
      </p>
      ${serving.length ? `
        <div class="small text-secondary mb-1">Models offered there right now:</div>
        <div>${serving.map((a) =>
          `<span class="badge text-bg-secondary me-1 mono">${esc(a.alias)}</span>`).join('')}</div>`
        : `<div class="alert alert-warning py-2 small mb-0">
             No alias is currently serving, so the chat model picker will be empty. Deploy
             a model and point an alias at it.
           </div>`}
      <a class="btn btn-sm btn-primary mt-3" href="${esc(chatUrl)}" target="_blank"
         rel="noopener">Open chat</a>
    </div>`;
}

/** Where Open WebUI lives, given where the admin console is being served from.
 *
 *  Production routes it by name on the same port (`chat.<host>`); a developer machine
 *  usually has no DNS entry, so the dev stack also publishes it on its own port. */
function chatHost() {
  const port = location.port;
  if (port && port !== '80' && port !== '443') {
    return `${location.hostname}:${CHAT_DEV_PORT}`;
  }
  return `chat.${location.hostname}`;
}

/* -------------------------------------------------------------------------- */
/* Agents (M10-M14)                                                           */
/* -------------------------------------------------------------------------- */
let toolCache = [];
let skillCache = [];

const RUN_STATE_CLASS = {
  COMPLETED: 'text-bg-success',
  FAILED: 'text-bg-danger',
  CANCELLED: 'text-bg-secondary',
  WAITING_FOR_APPROVAL: 'text-bg-warning',
};

async function renderAgents() {
  const agents = await api('/agents');
  if (!agents.length) {
    document.getElementById('agents-body').innerHTML = empty(
      'No agents. An agent is a model plus a prompt plus an explicit set of tools — ' +
      'create one and it appears in chat as agent:<slug>.');
    return;
  }

  // Fetched per agent because the list endpoint carries identity only; the version (and
  // so the tool grants) is what an operator actually needs to see here.
  const detailed = await Promise.all(agents.map((a) => api(`/agents/${a.id}`)));

  document.getElementById('agents-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Agent</th><th>Model</th><th>Tools</th><th>Skills</th>
          <th>Version</th><th>Runs</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${detailed.map(agentRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      Every agent is selectable in chat as <code>agent:&lt;slug&gt;</code>. A tool an agent
      holds is still refused unless the person asking also holds its permission.
    </div>`;

  wireAgentActions();
}

function agentRow(agent) {
  const version = agent.version || {};
  const tools = (version.tools || []);
  const unavailable = agent.unavailable_tools || [];

  return `<tr class="${agent.enabled ? '' : 'opacity-50'}">
    <td>
      <div class="fw-medium">${esc(agent.display_name)}</div>
      <div class="small text-secondary mono">agent:${esc(agent.slug)}</div>
      ${agent.enabled ? '' : '<span class="badge text-bg-dark">disabled</span>'}
    </td>
    <td class="small mono">${esc(version.model || '—')}</td>
    <td class="small">
      ${tools.length
        ? tools.map((n) => `<span class="badge text-bg-secondary me-1 mono">${esc(n)}</span>`).join('')
        : '<span class="text-secondary">none</span>'}
      ${unavailable.length
        ? `<div class="small text-warning mt-1" title="Granted but disabled, so not offered to the model">
             ${unavailable.length} granted but disabled</div>` : ''}
    </td>
    <td class="small">${(version.skills || []).length || '—'}</td>
    <td class="small">
      v${agent.current_version}
      ${version.change_note
        ? `<div class="text-secondary text-truncate" style="max-width:12rem"
                title="${esc(version.change_note)}">${esc(version.change_note)}</div>` : ''}
    </td>
    <td class="small">${agent.run_count}</td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary me-1" data-agent-runs="${esc(agent.id)}"
              data-agent-name="${esc(agent.display_name)}">Runs</button>
      <button class="btn btn-sm btn-outline-primary me-1" data-agent-try="${esc(agent.id)}"
              data-agent-name="${esc(agent.display_name)}">Try</button>
      <button class="btn btn-sm btn-outline-danger" data-agent-delete="${esc(agent.id)}"
              data-agent-name="${esc(agent.display_name)}">Delete</button>
    </td>
  </tr>`;
}

function wireAgentActions() {
  document.querySelectorAll('[data-agent-runs]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const runs = await api(`/agents/${button.dataset.agentRuns}/runs?limit=25`);
      showRunList(button.dataset.agentName, runs);
    });
  });

  document.querySelectorAll('[data-agent-try]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const message = prompt(`Ask ${button.dataset.agentName}:`,
        'Why is employee ABC123 locked out?');
      if (!message) return;
      const run = await api(`/agents/${button.dataset.agentTry}/execute`, {
        method: 'POST', body: JSON.stringify({ message }),
      });
      showRunTrace(run);
    });
  });

  document.querySelectorAll('[data-agent-delete]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Delete ${button.dataset.agentName}?\n\nIts runs are kept for audit.`)) return;
      const result = await api(`/agents/${button.dataset.agentDelete}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderAgents();
    });
  });
}

function showRunList(name, runs) {
  document.getElementById('run-modal-title').textContent = `Runs — ${name}`;
  document.getElementById('run-modal-body').innerHTML = runs.length ? `
    <table class="table table-sm align-middle mb-0">
      <thead><tr><th>When</th><th>Asked</th><th>State</th><th>Tokens</th><th></th></tr></thead>
      <tbody>${runs.map((r) => `
        <tr>
          <td class="small text-secondary">${esc(ago(r.created_at))}</td>
          <td class="small text-truncate" style="max-width:18rem">${esc(r.input)}</td>
          <td><span class="badge ${RUN_STATE_CLASS[r.state] || 'text-bg-info'}">${esc(r.state)}</span></td>
          <td class="small">${r.prompt_tokens + r.completion_tokens}</td>
          <td class="text-end">
            <button class="btn btn-sm btn-outline-secondary" data-run-trace="${esc(r.id)}">Trace</button>
          </td>
        </tr>`).join('')}
      </tbody>
    </table>` : empty('This agent has not been run yet.');

  new bootstrap.Modal(document.getElementById('run-modal')).show();
  document.querySelectorAll('[data-run-trace]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      showRunTrace(await api(`/runs/${button.dataset.runTrace}`));
    });
  });
}

/** The §11 trace. What the agent did, in order, with what it was refused. */
function showRunTrace(run) {
  document.getElementById('run-modal-title').textContent =
    `Run — ${run.agent_slug} (${run.state})`;

  document.getElementById('run-modal-body').innerHTML = `
    <div class="row g-3 mb-3">
      ${[['State', run.state], ['Iterations', run.iterations],
         ['Tokens', `${run.prompt_tokens}+${run.completion_tokens}`],
         ['Tool calls', run.tool_calls.length]].map(([label, value]) => `
        <div class="col-6 col-lg-3"><div class="stat-card">
          <div class="stat-value" style="font-size:1.1rem">${esc(value)}</div>
          <div class="stat-label">${esc(label)}</div>
        </div></div>`).join('')}
    </div>

    ${run.pending_tool ? `
      <div class="alert alert-warning py-2 small">
        Waiting for approval to run <code>${esc(run.pending_tool)}</code>.
        Approve it on the Approvals page.
      </div>` : ''}

    <div class="stat-card mb-3">
      <div class="stat-label mb-2">Asked</div>
      <div class="small">${esc(run.input)}</div>
    </div>

    ${run.output ? `
      <div class="stat-card mb-3">
        <div class="stat-label mb-2">Answered</div>
        <div class="small" style="white-space:pre-wrap">${esc(run.output)}</div>
      </div>` : ''}
    ${run.error ? `<div class="alert alert-danger py-2 small">${esc(run.error)}</div>` : ''}

    ${run.tool_calls.length ? `
      <div class="stat-label mb-2">Tool calls</div>
      <table class="table table-sm align-middle mb-3">
        <tbody>${run.tool_calls.map((c) => `
          <tr>
            <td class="mono small">${esc(c.tool_name)}</td>
            <td><span class="badge ${c.approval_state === 'REJECTED' ? 'text-bg-danger'
              : c.approval_state === 'PENDING' ? 'text-bg-warning' : 'text-bg-secondary'}"
              >${esc(c.approval_state)}</span></td>
            <td class="small">${esc(c.risk_level)}</td>
            <td class="small text-secondary">${c.duration_ms ? `${c.duration_ms.toFixed(0)}ms` : '—'}</td>
            <td class="small text-secondary text-truncate" style="max-width:16rem"
                title="${esc(c.decision_reason || '')}">${esc(c.decision_reason || '')}</td>
          </tr>`).join('')}
        </tbody>
      </table>` : ''}

    <div class="stat-label mb-2">Events (§11)</div>
    <pre class="logs">${run.events.map((e) =>
      `${String(e.sequence).padStart(3)}  ${e.type.padEnd(24)}` +
      `${e.duration_ms ? String(Math.round(e.duration_ms)).padStart(6) + 'ms' : '        '}  ` +
      `${esc(JSON.stringify(e.payload).slice(0, 160))}`).join('\n')}</pre>`;

  new bootstrap.Modal(document.getElementById('run-modal')).show();
}

/* -------------------------------------------------------------------------- */
/* Skills (M11)                                                               */
/* -------------------------------------------------------------------------- */
async function renderSkills() {
  // Tools are fetched too, only to flag a skill naming one that does not exist.
  const [skills, tools] = await Promise.all([api('/skills'), api('/tools')]);
  toolCache = tools;
  skillCache = skills;

  if (!skills.length) {
    document.getElementById('skills-body').innerHTML = empty(
      'No skills yet. A skill is reusable instructions — write "how to diagnose an ' +
      'account lockout" once and every agent that needs it can use the same text.');
    return;
  }

  document.getElementById('skills-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Skill</th><th>Instructions</th><th>Wants tools</th>
          <th>Version</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${skills.map(skillRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      A skill's instructions are appended to the agent's own prompt, so the agent's
      boundaries always win. Listing a tool here is advisory — the agent must still be
      granted it separately, and the person asking must still hold its permission.
    </div>`;

  document.querySelectorAll('[data-skill-delete]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Delete ${button.dataset.skillName}?\n\n` +
        'Agents using it keep their current version; the skill goes on their next edit.')) return;
      const result = await api(`/skills/${button.dataset.skillDelete}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderSkills();
    });
  });
}

function skillRow(skill) {
  const wanted = skill.required_tools || [];
  // Cross-checked against the registry: a skill naming a tool nobody has registered will
  // simply never work, and saying so here beats debugging it inside an agent run.
  const missing = toolCache.length
    ? wanted.filter((name) => !toolCache.some((t) => t.name === name))
    : [];

  return `<tr>
    <td>
      <div class="fw-medium">${esc(skill.display_name)}</div>
      <div class="small text-secondary mono">${esc(skill.name)}</div>
      <div class="small text-secondary">${esc(skill.description)}</div>
    </td>
    <td class="small text-secondary" style="max-width:24rem">
      <div class="text-truncate" title="${esc(skill.instructions)}">${esc(skill.instructions)}</div>
    </td>
    <td class="small">
      ${wanted.length
        ? wanted.map((n) => `<span class="badge ${missing.includes(n)
            ? 'text-bg-warning' : 'text-bg-secondary'} me-1 mono"
            title="${missing.includes(n) ? 'No tool with this name is registered' : ''}"
            >${esc(n)}</span>`).join('')
        : '<span class="text-secondary">—</span>'}
    </td>
    <td class="small">${esc(skill.version)}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-danger" data-skill-delete="${esc(skill.id)}"
              data-skill-name="${esc(skill.name)}">Delete</button>
    </td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* Tools and MCP (M12, M13)                                                   */
/* -------------------------------------------------------------------------- */
async function renderTools() {
  const [servers, tools] = await Promise.all([api('/mcp/servers'), api('/tools')]);
  toolCache = tools;

  document.getElementById('mcp-body').innerHTML = servers.length ? `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Server</th><th>Endpoint</th><th>Status</th><th>Tools</th>
          <th>Discovered</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${servers.map(mcpRow).join('')}</tbody>
      </table>
    </div>` : empty('No MCP servers. Register one to discover the tools it offers.');

  document.getElementById('tools-body').innerHTML = tools.length ? `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Tool</th><th>Type</th><th>Permission</th><th>Risk</th>
          <th>State</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${tools.map(toolRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      A discovered tool arrives disabled at HIGH risk. Review what it does, then enable it
      and set the risk deliberately — HIGH and CRITICAL suspend a run for human approval.
    </div>` : empty('No tools registered.');

  wireToolActions();
}

function mcpRow(server) {
  const status = {
    HEALTHY: 'text-bg-success', UNREACHABLE: 'text-bg-danger', ERROR: 'text-bg-danger',
  }[server.status] || 'text-bg-secondary';

  return `<tr>
    <td>
      <div class="fw-medium">${esc(server.name)}</div>
      <div class="small text-secondary">${esc(server.description || '')}</div>
    </td>
    <td class="small mono">${esc(server.endpoint)}</td>
    <td>
      <span class="badge ${status}">${esc(server.status)}</span>
      ${server.status_detail
        ? `<div class="small text-secondary">${esc(server.status_detail)}</div>` : ''}
    </td>
    <td class="small">${server.tool_count}</td>
    <td class="small text-secondary">${esc(ago(server.last_discovered_at))}</td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary me-1" data-mcp-check="${esc(server.id)}">Check</button>
      <button class="btn btn-sm btn-outline-primary me-1" data-mcp-discover="${esc(server.id)}">Discover</button>
      <button class="btn btn-sm btn-outline-danger" data-mcp-delete="${esc(server.id)}"
              data-mcp-name="${esc(server.name)}">Delete</button>
    </td>
  </tr>`;
}

const RISK_CLASS = {
  LOW: 'text-bg-secondary', MEDIUM: 'text-bg-info',
  HIGH: 'text-bg-warning', CRITICAL: 'text-bg-danger',
};

function toolRow(tool) {
  // PYTHON and COMMAND can be catalogued but never run (§25). Said plainly here, because
  // an operator who registers one and waits for it to work is owed an explanation.
  const neverRuns = tool.type === 'PYTHON' || tool.type === 'COMMAND';
  // A schema with no properties tells the model the tool takes no arguments, so it calls
  // it with none and the server rejects every call. Shown here because otherwise the only
  // symptom is an agent failing mid-conversation.
  const schema = tool.parameters_schema || {};
  const noParameters = !schema.properties && !schema.$ref && !schema.oneOf && !schema.anyOf;

  return `<tr class="${tool.enabled && !neverRuns ? '' : 'opacity-75'}">
    <td>
      <div class="fw-medium mono">${esc(tool.name)}</div>
      <div class="small text-secondary text-truncate" style="max-width:26rem"
           title="${esc(tool.description)}">${esc(tool.description)}</div>
      ${noParameters && !neverRuns
        ? '<div class="small text-warning" title="The server declares no parameters, so the '
          + 'model will call it with no arguments">declares no parameters</div>' : ''}
    </td>
    <td class="small">${esc(tool.type)}</td>
    <td class="small mono">${esc(tool.required_permission)}</td>
    <td>
      <span class="badge ${RISK_CLASS[tool.risk_level] || 'text-bg-secondary'}">${esc(tool.risk_level)}</span>
      ${tool.requires_approval
        ? '<div class="small text-secondary">needs approval</div>' : ''}
    </td>
    <td>
      ${neverRuns
        ? '<span class="badge text-bg-dark" title="Registerable but never executable (§25)">never runs</span>'
        : tool.enabled
          ? '<span class="badge text-bg-success">enabled</span>'
          : '<span class="badge text-bg-warning">needs review</span>'}
    </td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary me-1" data-tool-test="${esc(tool.id)}"
              ${neverRuns ? 'disabled' : ''}>Test</button>
      <button class="btn btn-sm btn-outline-primary me-1" data-tool-toggle="${esc(tool.id)}"
              data-enabled="${tool.enabled}" ${neverRuns ? 'disabled' : ''}
              >${tool.enabled ? 'Disable' : 'Enable'}</button>
      <select class="form-select form-select-sm d-inline-block" style="width:7rem"
              data-tool-risk="${esc(tool.id)}">
        ${['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].map((r) =>
          `<option ${r === tool.risk_level ? 'selected' : ''}>${r}</option>`).join('')}
      </select>
    </td>
  </tr>`;
}

function wireToolActions() {
  document.querySelectorAll('[data-mcp-check]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const server = await api(`/mcp/servers/${button.dataset.mcpCheck}/health`, { method: 'POST' });
      alert(`${server.name}: ${server.status} — ${server.status_detail || ''}`,
        server.status === 'HEALTHY' ? 'success' : 'warning');
      await renderTools();
    });
  });

  document.querySelectorAll('[data-mcp-discover]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const result = await api(`/mcp/servers/${button.dataset.mcpDiscover}/discover`, { method: 'POST' });
      alert(`${result.server_name}: ${result.found} offered, ${result.created} new, ` +
        `${result.updated} refreshed. ${result.detail || ''}`, 'success');
      await renderTools();
    });
  });

  document.querySelectorAll('[data-mcp-delete]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Delete ${button.dataset.mcpName}?\n\n` +
        'The tools discovered from it go too.')) return;
      const result = await api(`/mcp/servers/${button.dataset.mcpDelete}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderTools();
    });
  });

  document.querySelectorAll('[data-tool-test]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const result = await api(`/tools/${button.dataset.toolTest}/test`, { method: 'POST' });
      alert(result.message, 'success');
    });
  });

  document.querySelectorAll('[data-tool-toggle]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      await api(`/tools/${button.dataset.toolToggle}`, {
        method: 'PUT', body: JSON.stringify({ enabled: button.dataset.enabled !== 'true' }),
      });
      await renderTools();
    });
  });

  document.querySelectorAll('[data-tool-risk]').forEach((select) => {
    select.onchange = async () => {
      try {
        await api(`/tools/${select.dataset.toolRisk}`, {
          method: 'PUT', body: JSON.stringify({ risk_level: select.value }),
        });
        alert(`Risk level set to ${select.value}.`, 'success');
        await renderTools();
      } catch (err) {
        alert(err.message);
      }
    };
  });
}

/* -------------------------------------------------------------------------- */
/* Approvals (§10, §M24)                                                      */
/* -------------------------------------------------------------------------- */
async function renderApprovals() {
  let pending = [];
  try {
    pending = await api('/runs/pending-approvals');
  } catch (err) {
    // `tool.approve` is a distinct privilege from using tools, so an operator without it
    // is told why rather than shown an empty queue.
    document.getElementById('approvals-body').innerHTML = empty(
      `${err.message} Approving a privileged action requires the tool.approve permission.`);
    updateApprovalBadge(0);
    return;
  }

  updateApprovalBadge(pending.length);

  document.getElementById('approvals-body').innerHTML = pending.length ? `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Tool</th><th>Arguments</th><th>Risk</th><th>Waiting</th><th class="text-end">Decision</th>
        </tr></thead>
        <tbody>${pending.map(approvalRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      A refusal does not end the run — the agent is told and may answer another way. Both
      outcomes are audited against your account.
    </div>` : empty('Nothing is waiting for approval.');

  document.querySelectorAll('[data-approve]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const approved = button.dataset.approve === 'yes';
      const reason = prompt(approved ? 'Reason (optional):' : 'Why are you refusing?', '');
      if (!approved && reason === null) return;
      const run = await api(`/runs/${button.dataset.runId}/approve`, {
        method: 'POST', body: JSON.stringify({ approved, reason: reason || null }),
      });
      alert(`Run is now ${run.state}.`, 'success');
      await renderApprovals();
      showRunTrace(run);
    });
  });
}

function approvalRow(entry) {
  return `<tr>
    <td class="mono">${esc(entry.tool_name)}</td>
    <td class="small text-secondary text-truncate" style="max-width:20rem"
        title="${esc(JSON.stringify(entry.arguments))}">${esc(JSON.stringify(entry.arguments))}</td>
    <td><span class="badge ${RISK_CLASS[entry.risk_level] || 'text-bg-secondary'}">${esc(entry.risk_level)}</span></td>
    <td class="small text-secondary">${esc(ago(entry.requested_at))}</td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-success me-1" data-approve="yes"
              data-run-id="${esc(entry.run_id)}">Approve</button>
      <button class="btn btn-sm btn-outline-danger" data-approve="no"
              data-run-id="${esc(entry.run_id)}">Refuse</button>
    </td>
  </tr>`;
}

/** Surfaced in the sidebar: a run waiting on a human is stalled until someone looks. */
function updateApprovalBadge(count) {
  const badge = document.getElementById('approval-badge');
  badge.textContent = count;
  badge.classList.toggle('d-none', count === 0);
}

/* -------------------------------------------------------------------------- */
/* Nodes                                                                      */
/* -------------------------------------------------------------------------- */
/**
 * Invitations that have been issued but not yet taken up.
 *
 * Shown above the node table because a pending enrolment is a job half done, and the
 * common question is "did the script on that host work?". `last_error` is the answer: the
 * node-facing endpoint tells a caller nothing about why it refused, deliberately, so this
 * is the only place the reason surfaces.
 *
 * Expired rows are rendered greyed rather than dropped — an invitation that silently
 * vanishes reads as a bug.
 */
async function renderPendingEnrollments() {
  const host = document.getElementById('pending-enrollments');
  if (!host) return;

  let rows = [];
  try {
    rows = (await api('/node-enrollments?limit=50')).items
      .filter((e) => e.status === 'PENDING' || e.status === 'EXPIRED');
  } catch {
    host.innerHTML = '';           // never block the node table on this
    return;
  }
  if (!rows.length) { host.innerHTML = ''; return; }

  host.innerHTML = `
    <div class="card mb-3">
      <div class="card-header py-2 small text-secondary">Awaiting enrolment</div>
      <div class="table-responsive">
        <table class="table table-sm mb-0 align-middle">
          <thead><tr>
            <th>Node</th><th>Token</th><th>Status</th><th>Expires</th><th>Last attempt</th><th></th>
          </tr></thead>
          <tbody>${rows.map(enrollmentRow).join('')}</tbody>
        </table>
      </div>
    </div>`;

  host.querySelectorAll('[data-revoke]').forEach((button) => {
    button.onclick = async () => {
      if (!confirm(`Revoke the enrolment for ${button.dataset.node}? The token stops working.`)) return;
      await api(`/node-enrollments/${button.dataset.revoke}`, { method: 'DELETE' });
      await renderNodes();
    };
  });
}

function enrollmentRow(e) {
  const expired = e.status === 'EXPIRED';
  const expiry = expired
    ? 'expired'
    : `in ${Math.max(0, Math.round((new Date(e.expires_at) - Date.now()) / 60000))} min`;
  // The reason it has not enrolled yet is the whole point of this row.
  const attempt = e.last_error
    ? `<span class="text-warning">${esc(e.last_error.slice(0, 90))}</span>`
    : `<span class="text-secondary">${e.attempts ? `${e.attempts} failed` : 'not yet contacted'}</span>`;
  return `
    <tr class="${expired ? 'opacity-50' : ''}">
      <td>${esc(e.node_name)}</td>
      <td><code class="small">${esc(e.token_prefix)}…</code></td>
      <td><span class="badge bg-secondary">${esc(e.status)}</span></td>
      <td class="small">${esc(expiry)}</td>
      <td class="small">${attempt}</td>
      <td class="text-end">
        <button class="btn btn-sm btn-outline-danger" data-revoke="${esc(e.id)}"
                data-node="${esc(e.node_name)}">Revoke</button>
      </td>
    </tr>`;
}

async function renderNodes() {
  const page = await api('/nodes?limit=100');
  const nodes = page.items;
  await renderPendingEnrollments();

  const online = nodes.filter((n) => n.status === 'ONLINE').length;
  const gpus = nodes.reduce((sum, n) => sum + n.gpus.length, 0);
  const synthetic = nodes.filter((n) => n.gpu_synthetic).length;

  document.getElementById('node-stats').innerHTML = [
    ['Nodes', nodes.length],
    ['Online', `${online} / ${nodes.length}`],
    ['GPUs', gpus],
    ['Synthetic nodes', synthetic],
  ].map(([label, value]) => `
    <div class="col-6 col-lg-3">
      <div class="stat-card">
        <div class="stat-value">${esc(value)}</div>
        <div class="stat-label">${esc(label)}</div>
      </div>
    </div>`).join('');

  if (!nodes.length) {
    document.getElementById('nodes-body').innerHTML =
      empty('No nodes yet. Add one to get an install command to run on the host.');
    return;
  }

  document.getElementById('nodes-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Node</th><th>Status</th><th>Host</th><th>CPU / Memory</th>
          <th>Docker</th><th>GPUs</th><th>Last seen</th><th></th>
        </tr></thead>
        <tbody>${nodes.map(nodeRow).join('')}</tbody>
      </table>
    </div>`;

  document.querySelectorAll('[data-check-node]').forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        const result = await api(`/nodes/${button.dataset.checkNode}/health`, { method: 'POST' });
        alert(
          `${result.node_name}: ${result.status}` +
          (result.error ? ` — ${result.error}` :
            ` (${result.gpus_seen} GPUs, ${result.containers_seen} containers)`),
          result.status === 'ONLINE' ? 'success' : 'warning',
        );
        await renderNodes();
      } catch (err) {
        alert(err.message);
      } finally {
        button.disabled = false;
      }
    };
  });
}

function nodeRow(node) {
  const detail = node.status_detail
    ? `<div class="small text-secondary">${esc(node.status_detail)}</div>` : '';
  // Synthetic telemetry is called out on the node itself, not only on the GPU page:
  // an operator glancing at the fleet must not mistake a fake node for capacity.
  const synthetic = node.gpu_synthetic
    ? '<span class="badge badge-synthetic ms-1" title="This node reports fabricated GPU telemetry">SYNTHETIC</span>' : '';

  return `<tr>
    <td>
      <div class="fw-medium">${esc(node.name)}${synthetic}</div>
      <div class="small text-secondary mono">${esc(node.agent_url)}</div>
    </td>
    <td>${dot(node.status)}${esc(node.status)}${detail}</td>
    <td class="small">
      ${esc(node.hostname || '—')}
      <div class="text-secondary">${esc(node.os_info || '')} ${esc(node.architecture || '')}</div>
    </td>
    <td class="small">
      ${esc(node.cpu_cores || '—')} cores
      <div class="text-secondary">${mib(node.memory_total_mib)}</div>
    </td>
    <td class="small">
      ${esc(node.docker_version || '—')}
      <div class="text-secondary">${node.nvidia_runtime_available ? 'nvidia runtime' : 'no nvidia runtime'}</div>
    </td>
    <td>
      ${node.gpus.length}
      ${node.nvidia_driver_version
        ? `<div class="small text-secondary">${esc(node.nvidia_driver_version)} / CUDA ${esc(node.cuda_version || '?')}</div>` : ''}
    </td>
    <td class="small text-secondary">${esc(ago(node.last_seen_at))}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-secondary" data-check-node="${esc(node.id)}">Check</button>
    </td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* GPUs                                                                       */
/* -------------------------------------------------------------------------- */
async function renderGpus() {
  const gpus = await api('/gpus');

  if (!gpus.length) {
    document.getElementById('gpus-body').innerHTML =
      empty('No GPUs. Register a node with GPUs to see telemetry here.');
    return;
  }

  document.getElementById('gpus-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>#</th><th>Model</th><th>Health</th><th>Utilisation</th>
          <th>Memory</th><th>Temp</th><th>Power</th><th>Allocated</th>
        </tr></thead>
        <tbody>${gpus.map(gpuRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">Select a GPU for its utilisation history.</div>`;

  document.querySelectorAll('.gpu-row').forEach((row) => {
    row.onclick = () => showGpuChart(row.dataset.gpuId, row.dataset.gpuName);
  });
}

function gpuRow(gpu) {
  const m = gpu.latest_metric;
  if (!m) {
    return `<tr class="gpu-row" data-gpu-id="${esc(gpu.id)}" data-gpu-name="${esc(gpu.name)}">
      <td>${gpu.index}</td><td>${esc(gpu.name)}</td>
      <td>${dot('UNKNOWN')}no data</td>
      <td colspan="5" class="small text-secondary">Awaiting first collection…</td>
    </tr>`;
  }
  return `<tr class="gpu-row" data-gpu-id="${esc(gpu.id)}" data-gpu-name="${esc(gpu.name)}">
    <td>${gpu.index}</td>
    <td>
      ${esc(gpu.name)}
      <div class="small text-secondary mono">${esc(gpu.uuid.slice(0, 20))}…</div>
    </td>
    <td>${dot(m.health)}${esc(m.health)}</td>
    <td style="min-width:8rem">
      ${meter(m.utilization_percent)}
      <div class="small text-secondary">${m.utilization_percent.toFixed(0)}%</div>
    </td>
    <td style="min-width:8rem">
      ${meter(m.memory_utilization_percent)}
      <div class="small text-secondary">${mib(m.memory_used_mib)} / ${mib(m.memory_total_mib)}</div>
    </td>
    <td class="small">${m.temperature_celsius.toFixed(0)}°C</td>
    <td class="small">${m.power_draw_watts.toFixed(0)} W${
      m.power_limit_watts ? ` <span class="text-secondary">/ ${m.power_limit_watts.toFixed(0)}</span>` : ''}</td>
    <td>${gpu.allocated
      ? '<span class="badge text-bg-warning">reserved</span>'
      : '<span class="badge text-bg-secondary">free</span>'}</td>
  </tr>`;
}

async function showGpuChart(gpuId, gpuName) {
  const modal = new bootstrap.Modal(document.getElementById('gpu-modal'));
  document.getElementById('gpu-modal-title').textContent = gpuName;
  modal.show();

  const series = await api(`/gpus/${gpuId}/metrics?since_minutes=60&limit=240`);
  const labels = series.samples.map((s) =>
    new Date(s.recorded_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));

  if (gpuChart) gpuChart.destroy();
  gpuChart = new Chart(document.getElementById('gpu-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Utilisation %', data: series.samples.map((s) => s.utilization_percent),
          borderColor: '#2ea043', tension: .3, pointRadius: 0, borderWidth: 2 },
        { label: 'Memory %', data: series.samples.map((s) => s.memory_utilization_percent),
          borderColor: '#58a6ff', tension: .3, pointRadius: 0, borderWidth: 2 },
        { label: 'Temp °C', data: series.samples.map((s) => s.temperature_celsius),
          borderColor: '#d29922', tension: .3, pointRadius: 0, borderWidth: 2 },
      ],
    },
    options: {
      responsive: true,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: { y: { beginAtZero: true, suggestedMax: 100 } },
      plugins: { legend: { labels: { boxWidth: 12 } } },
    },
  });

  document.getElementById('gpu-modal-meta').textContent =
    `${series.samples.length} samples over the last hour.` +
    (series.samples.length ? '' : ' Collection may not have run yet.');
}

/* -------------------------------------------------------------------------- */
/* Containers                                                                 */
/* -------------------------------------------------------------------------- */
async function renderContainers() {
  const managedOnly = document.getElementById('managed-only').checked;
  const containers = await api(`/containers?managed_only=${managedOnly}`);

  if (!containers.length) {
    document.getElementById('containers-body').innerHTML = empty(
      managedOnly
        ? 'No platform-managed containers. Models deployed in Phase 2 will appear here.'
        : 'No containers.');
    return;
  }

  document.getElementById('containers-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Name</th><th>Image</th><th>State</th><th>Managed</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${containers.map(containerRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      Containers the platform did not create are shown for visibility but cannot be
      controlled — the node refuses, which is what stops the platform stopping its own
      database.
    </div>`;

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.onclick = async () => {
      const { action, cid, cname } = button.dataset;
      if (action === 'remove' && !confirm(`Remove container ${cname}?`)) return;
      button.disabled = true;
      try {
        const method = action === 'remove' ? 'DELETE' : 'POST';
        const path = action === 'remove' ? `/containers/${cid}` : `/containers/${cid}/${action}`;
        const result = await api(path, { method });
        alert(result.message, 'success');
        await renderContainers();
      } catch (err) {
        alert(err.message);
      } finally {
        button.disabled = false;
      }
    };
  });
}

function containerRow(container) {
  const controls = container.managed
    ? ['start', 'stop', 'restart'].map((action) =>
        `<button class="btn btn-sm btn-outline-secondary me-1"
                 data-action="${action}" data-cid="${esc(container.container_id)}"
                 data-cname="${esc(container.name)}">${action}</button>`).join('') +
      `<button class="btn btn-sm btn-outline-danger"
               data-action="remove" data-cid="${esc(container.container_id)}"
               data-cname="${esc(container.name)}">remove</button>`
    : '<span class="small text-secondary">not managed</span>';

  return `<tr>
    <td>
      <div class="fw-medium">${esc(container.name)}</div>
      <div class="small text-secondary mono">${esc(container.container_id.slice(0, 12))}</div>
    </td>
    <td class="small mono">${esc(container.image)}</td>
    <td>${dot(container.state)}${esc(container.state)}</td>
    <td>${container.managed
      ? '<span class="badge text-bg-success">yes</span>'
      : '<span class="badge text-bg-secondary">no</span>'}</td>
    <td class="text-end">${controls}</td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* Model registry (M07)                                                       */
/* -------------------------------------------------------------------------- */

/** Cached so the deploy and alias modals can populate without a second fetch. */
let modelCache = [];

async function renderModels() {
  const page = await api('/models?limit=200');
  modelCache = page.items;

  if (!modelCache.length) {
    document.getElementById('models-body').innerHTML = empty(
      'No models registered. Import the manifests that ship with the bundle, or ' +
      'register one by hand.');
    return;
  }

  document.getElementById('models-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Model</th><th>Type</th><th>Runtime</th><th>Context</th>
          <th>Requires</th><th>Status</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${modelCache.map(modelRow).join('')}</tbody>
      </table>
    </div>`;

  wireModelActions();
}

/* AVAILABLE is the only status that can serve — anything else is called out in colour
 * so an operator does not go hunting through a deploy failure to learn the weights
 * were never on disk. */
const MODEL_STATUS_CLASS = {
  AVAILABLE: 'text-bg-success',
  REGISTERED: 'text-bg-secondary',
  UNAVAILABLE: 'text-bg-danger',
  VERIFYING: 'text-bg-info',
};

/** An external runtime is one the platform points at rather than runs. Marked in the
 *  table because every lifecycle control on the row does nothing for it, and a row that
 *  looks identical to a managed one invites an operator to try. */
const isExternal = (model) => Boolean(model.endpoint_url);

function modelRow(model) {
  const badge = MODEL_STATUS_CLASS[model.status] || 'text-bg-secondary';
  const detail = model.status_detail
    ? `<div class="small text-secondary">${esc(model.status_detail)}</div>` : '';
  const deployable = model.status === 'AVAILABLE';

  return `<tr>
    <td>
      <div class="fw-medium">${esc(model.name)}
        ${isExternal(model)
          ? '<span class="badge text-bg-info ms-1" title="Served by a runtime the platform '
            + 'does not start, stop or schedule">external</span>' : ''}
      </div>
      <div class="small text-secondary">${esc(model.display_name)}</div>
      <div class="small text-secondary mono">${esc(model.storage_path)}</div>
      ${isExternal(model)
        ? `<div class="small text-secondary">runs at ${esc(model.endpoint_url)}</div>` : ''}
    </td>
    <td class="small">${esc(model.type)}
      <div class="small text-secondary">${esc(model.runtime)}</div>
    </td>
    <td class="small mono">${esc(model.runtime)}</td>
    <td class="small">${model.context_length ? model.context_length.toLocaleString() : '—'}</td>
    <td class="small">
      ${model.min_gpu_count} GPU${model.min_gpu_count === 1 ? '' : 's'}
      <div class="text-secondary">${mib(model.required_gpu_memory_mib)} each</div>
    </td>
    <td>
      <span class="badge ${badge}">${esc(model.status)}</span>${detail}
    </td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary me-1" data-import-model="${esc(model.id)}"
              title="Scan the storage path and verify checksums">Verify</button>
      <button class="btn btn-sm btn-outline-primary me-1" data-deploy-model="${esc(model.id)}"
              data-model-name="${esc(model.name)}" ${deployable ? '' : 'disabled'}
              title="${deployable ? 'Deploy this model' : 'Only an AVAILABLE model can be deployed'}"
              >Deploy</button>
      <button class="btn btn-sm btn-outline-danger" data-delete-model="${esc(model.id)}"
              data-model-name="${esc(model.name)}">Delete</button>
    </td>
  </tr>`;
}

function wireModelActions() {
  document.querySelectorAll('[data-import-model]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const result = await api(`/models/${button.dataset.importModel}/import`, { method: 'POST' });
      alert(
        `${result.model_name}: ${result.status} — ${result.files_found} files, ` +
        `${result.files_hashed} hashed, ${mib(result.total_bytes / 1048576)}` +
        (result.detail ? ` (${result.detail})` : ''),
        result.status === 'AVAILABLE' ? 'success' : 'warning');
      await renderModels();
    });
  });

  document.querySelectorAll('[data-deploy-model]').forEach((button) => {
    button.onclick = () => openDeployModal(button.dataset.deployModel, button.dataset.modelName);
  });

  document.querySelectorAll('[data-delete-model]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(
        `Remove ${button.dataset.modelName} from the registry?\n\n` +
        'Files on disk are untouched.')) return;
      const result = await api(`/models/${button.dataset.deleteModel}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderModels();
    });
  });
}

/** Run an action with the button disabled, surfacing any failure as an alert. */
async function guard(button, action) {
  button.disabled = true;
  try {
    await action();
  } catch (err) {
    alert(err.message);
  } finally {
    button.disabled = false;
  }
}

async function openDeployModal(modelId, modelName) {
  const form = document.getElementById('deploy-form');
  form.dataset.modelId = modelId;
  document.getElementById('deploy-model-name').textContent = modelName;
  document.getElementById('deploy-error').classList.add('d-none');

  const nodes = (await api('/nodes?limit=100')).items.filter((n) => n.status === 'ONLINE');
  document.getElementById('dep-node').innerHTML =
    '<option value="">Let the scheduler choose</option>' +
    nodes.map((n) =>
      `<option value="${esc(n.id)}">${esc(n.name)} — ${n.gpus.length} GPU${
        n.gpus.length === 1 ? '' : 's'}</option>`).join('');

  new bootstrap.Modal(document.getElementById('deploy-modal')).show();
}

/* -------------------------------------------------------------------------- */
/* Deployments (M08)                                                          */
/* -------------------------------------------------------------------------- */

/* The §M08 lifecycle. Anything not terminal is still moving, so the row shows a
 * spinner rather than a static badge — a deployment that takes four minutes to load
 * otherwise looks indistinguishable from one that has hung. */
const TERMINAL_STATES = ['RUNNING', 'FAILED', 'STOPPED'];
const STATE_CLASS = { RUNNING: 'text-bg-success', FAILED: 'text-bg-danger', STOPPED: 'text-bg-secondary' };

async function renderDeployments() {
  const deployments = await api('/deployments');

  const running = deployments.filter((d) => d.state === 'RUNNING').length;
  const failed = deployments.filter((d) => d.state === 'FAILED').length;
  const moving = deployments.filter((d) => !TERMINAL_STATES.includes(d.state)).length;
  const gpus = deployments
    .filter((d) => !['STOPPED', 'FAILED'].includes(d.state))
    .reduce((sum, d) => sum + d.gpu_indices.length, 0);

  document.getElementById('deployment-stats').innerHTML = [
    ['Running', running], ['In progress', moving], ['Failed', failed], ['GPUs held', gpus],
  ].map(([label, value]) => `
    <div class="col-6 col-lg-3">
      <div class="stat-card">
        <div class="stat-value">${esc(value)}</div>
        <div class="stat-label">${esc(label)}</div>
      </div>
    </div>`).join('');

  if (!deployments.length) {
    document.getElementById('deployments-body').innerHTML =
      empty('Nothing deployed. Deploy an AVAILABLE model from the registry.');
    return;
  }

  document.getElementById('deployments-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Model</th><th>Node / GPUs</th><th>State</th><th>Runtime</th>
          <th>Started</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${deployments.map(deploymentRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      Deployments are driven by a background worker and progress through the lifecycle on
      their own. The container address is deliberately not shown — callers reach a model
      through the gateway, never directly.
    </div>`;

  wireDeploymentActions();
}

function deploymentRow(deployment) {
  const moving = !TERMINAL_STATES.includes(deployment.state);
  const badge = STATE_CLASS[deployment.state] || 'text-bg-info';
  const state = moving
    ? `<span class="badge text-bg-info">
         <span class="spinner-border spinner-border-sm me-1" style="width:.6rem;height:.6rem"></span>
         ${esc(deployment.state)}</span>`
    : `<span class="badge ${badge}">${esc(deployment.state)}</span>`;

  const explanation = deployment.error_message || deployment.state_detail;
  const detail = explanation
    ? `<div class="small text-secondary text-truncate" style="max-width:22rem"
            title="${esc(explanation)}">${esc(explanation)}</div>` : '';

  const stoppable = !['STOPPED', 'FAILED'].includes(deployment.state);

  return `<tr>
    <td>
      <div class="fw-medium">${esc(deployment.model_name)}</div>
      <div class="small text-secondary mono">${esc(deployment.id.slice(0, 8))}</div>
    </td>
    <td class="small">
      ${esc(deployment.node_name || '—')}
      <div class="text-secondary mono">${
        deployment.gpu_indices.length ? `GPU ${deployment.gpu_indices.join(', ')}` : 'no GPUs'}</div>
    </td>
    <td>${state}${detail}</td>
    <td class="small mono">
      ${esc(deployment.runtime)}
      <div class="text-secondary">tp=${deployment.tensor_parallel_size}</div>
    </td>
    <td class="small text-secondary">${esc(ago(deployment.started_at || deployment.created_at))}</td>
    <td class="text-end text-nowrap">
      <button class="btn btn-sm btn-outline-secondary me-1"
              data-logs="${esc(deployment.id)}" data-dep-name="${esc(deployment.model_name)}"
              >Logs</button>
      <button class="btn btn-sm btn-outline-secondary me-1"
              data-dep-action="restart" data-dep="${esc(deployment.id)}"
              ${stoppable ? '' : 'disabled'}>Restart</button>
      <button class="btn btn-sm btn-outline-warning me-1"
              data-dep-action="stop" data-dep="${esc(deployment.id)}"
              ${stoppable ? '' : 'disabled'}>Stop</button>
      <button class="btn btn-sm btn-outline-danger" data-delete-dep="${esc(deployment.id)}">Delete</button>
    </td>
  </tr>`;
}

function wireDeploymentActions() {
  document.querySelectorAll('[data-dep-action]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const { depAction, dep } = button.dataset;
      const result = await api(`/deployments/${dep}/${depAction}`, { method: 'POST' });
      alert(`Deployment is now ${result.state}.`, 'success');
      await renderDeployments();
    });
  });

  document.querySelectorAll('[data-delete-dep]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm('Stop and remove this deployment?')) return;
      const result = await api(`/deployments/${button.dataset.deleteDep}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderDeployments();
    });
  });

  document.querySelectorAll('[data-logs]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      document.getElementById('logs-modal-title').textContent = `Logs — ${button.dataset.depName}`;
      const body = document.getElementById('logs-body');
      body.textContent = 'Loading…';
      new bootstrap.Modal(document.getElementById('logs-modal')).show();
      const result = await api(`/deployments/${button.dataset.logs}/logs?tail=500`);
      body.textContent = result.lines || '(no output)';
      body.scrollTop = body.scrollHeight;
    });
  });
}

/* -------------------------------------------------------------------------- */
/* Endpoints, keys and usage (§13, M20)                                       */
/* -------------------------------------------------------------------------- */
async function renderEndpoints() {
  const [aliases, keys, clients, usage, models] = await Promise.all([
    api('/model-aliases'), api('/api-keys'), api('/api-clients'),
    api('/usage?since_hours=24'), api('/models?limit=200'),
  ]);
  modelCache = models.items;

  renderAliases(aliases);
  renderKeys(keys, clients);
  renderUsage(usage);
}

function renderAliases(aliases) {
  const body = document.getElementById('aliases-body');
  if (!aliases.length) {
    body.innerHTML = empty(
      'No aliases. An alias is the stable name developers code against — repointing it ' +
      'swaps the model underneath without any application changing.');
    return;
  }

  body.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Alias</th><th>Target model</th><th>Serving</th><th>Enabled</th>
          <th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${aliases.map(aliasRow).join('')}</tbody>
      </table>
    </div>`;

  document.querySelectorAll('[data-repoint]').forEach((select) => {
    select.onchange = async () => {
      try {
        await api(`/model-aliases/${select.dataset.repoint}`, {
          method: 'PUT', body: JSON.stringify({ model_id: select.value }),
        });
        alert('Alias repointed. Callers pick it up on their next request.', 'success');
        await renderEndpoints();
      } catch (err) {
        alert(err.message);
        await renderEndpoints();
      }
    };
  });

  document.querySelectorAll('[data-delete-alias]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(
        `Delete alias ${button.dataset.aliasName}?\n\n` +
        'Any application calling it will start getting 404s.')) return;
      await api(`/model-aliases/${button.dataset.deleteAlias}`, { method: 'DELETE' });
      alert('Alias deleted.', 'success');
      await renderEndpoints();
    });
  });
}

function aliasRow(alias) {
  const options = modelCache.map((m) =>
    `<option value="${esc(m.id)}" ${m.id === alias.model_id ? 'selected' : ''}>${esc(m.name)}</option>`
  ).join('');

  return `<tr>
    <td class="mono fw-medium">${esc(alias.alias)}</td>
    <td>
      <select class="form-select form-select-sm" data-repoint="${esc(alias.id)}"
              style="max-width:16rem">${options}</select>
    </td>
    <td>${alias.serving
      ? '<span class="badge text-bg-success">yes</span>'
      : '<span class="badge text-bg-warning" title="The target model is not deployed — calls return 503">no</span>'}</td>
    <td>${alias.enabled
      ? '<span class="badge text-bg-secondary">enabled</span>'
      : '<span class="badge text-bg-dark">disabled</span>'}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-danger" data-delete-alias="${esc(alias.id)}"
              data-alias-name="${esc(alias.alias)}">Delete</button>
    </td>
  </tr>`;
}

function renderKeys(keys, clients) {
  const byId = Object.fromEntries(clients.map((c) => [c.id, c.name]));
  document.getElementById('key-client').innerHTML =
    clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');

  const body = document.getElementById('keys-body');
  if (!keys.length) {
    body.innerHTML = empty('No API keys. A key is how a developer application authenticates ' +
      'to the gateway — independently revocable, and never tied to a person\'s account.');
    return;
  }

  body.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Key</th><th>Client</th><th>Scopes</th><th>Rate limit</th><th>Last used</th>
          <th>Status</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${keys.map((k) => keyRow(k, byId)).join('')}</tbody>
      </table>
    </div>`;

  document.querySelectorAll('[data-rotate-key]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const answer = prompt(
        `Rotate "${button.dataset.keyName}".\n\n` +
        'Hours the old key keeps working. It stops on its own after that, so you can ' +
        'rotate first and redeploy afterwards.\n\n' +
        'Use 0 only for a compromised key — that breaks callers immediately.', '24');
      if (answer === null) return;
      const result = await api(`/api-keys/${button.dataset.rotateKey}/rotate`, {
        method: 'POST',
        body: JSON.stringify({ grace_hours: Number(answer) || 0 }),
      });
      // Shown in a prompt rather than an alert banner: this is the only time the key
      // exists anywhere the operator can read it, and a banner that auto-dismisses
      // after four seconds would lose it.
      window.prompt(result.message, result.api_key);
      await renderEndpoints();
    });
  });

  document.querySelectorAll('[data-revoke-key]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm('Revoke this key?\n\nCalls using it start failing immediately.')) return;
      const result = await api(`/api-keys/${button.dataset.revokeKey}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await renderEndpoints();
    });
  });
}

function keyRow(key, clientNames) {
  const revoked = Boolean(key.revoked_at);
  const expired = key.expires_at && new Date(key.expires_at) < new Date();

  return `<tr class="${revoked ? 'opacity-50' : ''}">
    <td>
      <div class="fw-medium">${esc(key.name)}</div>
      <div class="small text-secondary mono">${esc(key.prefix)}…</div>
    </td>
    <td class="small">${esc(clientNames[key.client_id] || '—')}</td>
    <td class="small">
      ${(key.scopes || []).length
        ? key.scopes.map((s) => `<span class="badge text-bg-secondary me-1 mono">${esc(s)}</span>`).join('')
        // Said plainly rather than left blank: an empty scope list is a key that can call
        // anything, and a blank cell reads as "not configured yet".
        : '<span class="text-secondary">unrestricted</span>'}
    </td>
    <td class="small">${key.rate_limit_per_minute}/min</td>
    <td class="small text-secondary">${esc(ago(key.last_used_at))}</td>
    <td>${revoked
      ? '<span class="badge text-bg-danger">revoked</span>'
      : expired
        ? '<span class="badge text-bg-warning">expired</span>'
        : '<span class="badge text-bg-success">active</span>'}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-secondary" data-rotate-key="${esc(key.id)}"
              data-key-name="${esc(key.name)}" ${revoked ? 'disabled' : ''}>Rotate</button>
      <button class="btn btn-sm btn-outline-danger ms-1" data-revoke-key="${esc(key.id)}"
              ${revoked ? 'disabled' : ''}>Revoke</button>
    </td>
  </tr>`;
}

function renderUsage(usage) {
  const body = document.getElementById('usage-body');
  if (!usage.rows.length) {
    body.innerHTML = empty('No gateway traffic in the last 24 hours.');
    return;
  }

  body.innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Model</th><th class="text-end">Requests</th><th class="text-end">Prompt tokens</th>
          <th class="text-end">Completion tokens</th><th class="text-end">Avg latency</th>
        </tr></thead>
        <tbody>${usage.rows.map((row) => `
          <tr>
            <td class="mono">${esc(row.model)}</td>
            <td class="text-end">${row.requests.toLocaleString()}</td>
            <td class="text-end">${row.prompt_tokens.toLocaleString()}</td>
            <td class="text-end">${row.completion_tokens.toLocaleString()}</td>
            <td class="text-end">${row.avg_latency_ms.toFixed(0)} ms</td>
          </tr>`).join('')}
        </tbody>
        <tfoot><tr class="fw-medium border-top">
          <td>Total</td>
          <td class="text-end">${usage.total_requests.toLocaleString()}</td>
          <td class="text-end" colspan="2">${usage.total_tokens.toLocaleString()} tokens</td>
          <td></td>
        </tr></tfoot>
      </table>
    </div>
    <div class="small text-secondary">
      Streamed requests are counted too — the usage chunk is read as it passes, so the
      response is never buffered to measure it.
    </div>`;
}

/* -------------------------------------------------------------------------- */
/* Voice assistant (M29 §49)                                                   */
/* -------------------------------------------------------------------------- */

/** Configuration the platform applies to the *next* session, not the next restart.
 *
 * The page deliberately shows readiness before settings: "enabled" is meaningless if no
 * speech model is serving, and an operator who ticks the box and then finds sessions
 * failing has been told the wrong thing by this screen.
 */
async function renderVoice() {
  const [config, models, deployments, agents] = await Promise.all([
    api('/voice/config'),
    api('/models?limit=200'),
    api('/deployments?limit=200'),
    api('/agents'),
  ]);

  const items = models.items || models;
  const running = new Set(
    (deployments.items || deployments)
      .filter((d) => d.state === 'RUNNING')
      .map((d) => d.model_id || d.model),
  );
  const byType = (type) => items.filter((m) => m.type === type);
  const isServing = (model) => running.has(model.id) || running.has(model.name);

  fillModelOptions('vc-stt', byType('ASR'), config.stt_model, isServing);
  fillModelOptions('vc-tts', byType('TTS'), config.tts_model, isServing);

  const agentList = agents.items || agents;
  document.getElementById('vc-agent').innerHTML =
    ['<option value="">— none —</option>']
      .concat(agentList.map((a) =>
        `<option value="${esc(a.slug)}" ${a.slug === config.default_agent_slug ? 'selected' : ''}
          >${esc(a.display_name)} (${esc(a.slug)})</option>`))
      .join('');

  setChecked('vc-enabled', config.enabled);
  setValue('vc-language', config.default_language);
  setValue('vc-voice', config.default_voice);
  setValue('vc-rate', config.sample_rate_hz);
  setValue('vc-max', config.max_session_seconds);
  setValue('vc-idle', config.idle_timeout_seconds);
  setChecked('vc-interrupt', config.interrupt_enabled);
  setChecked('vc-vad', config.vad_enabled);
  setChecked('vc-store-audio', config.store_audio);
  setChecked('vc-store-transcripts', config.store_transcripts);
  setValue('vc-retention', config.retention_days);

  document.getElementById('voice-state-badge').innerHTML = config.enabled
    ? '<span class="badge text-bg-success">enabled</span>'
    : '<span class="badge text-bg-secondary">disabled</span>';

  renderVoiceReadiness(config, byType('ASR'), byType('TTS'), isServing);
  renderVoiceEndpoints(byType('ASR').concat(byType('TTS')), isServing);
}

/** What is missing before a session can work, said plainly.
 *
 * Each line is actionable. "No ASR model is serving" sends somebody to the right page;
 * a session that closes with a socket error does not.
 */
function renderVoiceReadiness(config, asr, tts, isServing) {
  const problems = [];
  const servingAsr = asr.filter(isServing);
  const servingTts = tts.filter(isServing);

  if (!asr.length) problems.push('No speech-to-text model is registered. Register one under Models.');
  else if (!servingAsr.length) problems.push('A speech-to-text model is registered but none is serving. Deploy one, or point it at a running endpoint.');
  if (!tts.length) problems.push('No text-to-speech model is registered.');
  else if (!servingTts.length) problems.push('A text-to-speech model is registered but none is serving. Without it the assistant answers in text only.');
  if (!config.default_agent_slug) problems.push('No default agent is set, so every caller must name one.');

  const element = document.getElementById('voice-readiness');
  if (!problems.length) {
    element.innerHTML = `<div class="alert alert-success py-2 small mb-0">
      Ready: ${servingAsr.length} speech-to-text and ${servingTts.length} text-to-speech
      model(s) serving.</div>`;
    return;
  }
  element.innerHTML = `<div class="alert alert-warning py-2 small mb-0">
    <div class="fw-medium mb-1">Not ready yet</div>
    ${problems.map((p) => `<div>• ${esc(p)}</div>`).join('')}
  </div>`;
}

function renderVoiceEndpoints(models, isServing) {
  if (!models.length) {
    document.getElementById('voice-endpoints').innerHTML =
      `<div class="text-secondary small">No speech models registered yet.</div>`;
    return;
  }
  document.getElementById('voice-endpoints').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Model</th><th>Type</th><th>Runtime</th><th>Where it runs</th><th>State</th>
        </tr></thead>
        <tbody>${models.map((m) => `<tr>
          <td class="fw-medium">${esc(m.display_name)}<div class="small text-secondary mono">${esc(m.name)}</div></td>
          <td><span class="badge text-bg-secondary">${esc(m.type)}</span></td>
          <td class="small">${esc(m.runtime)}</td>
          <td class="small mono">${m.endpoint_url
            ? esc(m.endpoint_url)
            : '<span class="text-secondary">deployed by the platform</span>'}</td>
          <td class="small">${isServing(m)
            ? '<span class="text-success">serving</span>'
            : '<span class="text-secondary">not serving</span>'}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>`;
}

/** Options for a model select, marking which are actually serving.
 *
 * A model that is registered but not deployed is still selectable — an operator often
 * configures before deploying — but it is labelled, so the choice is informed.
 */
function fillModelOptions(id, models, selected, isServing) {
  const element = document.getElementById(id);
  const options = models.map((m) =>
    `<option value="${esc(m.name)}" ${m.name === selected ? 'selected' : ''}
      >${esc(m.name)}${isServing(m) ? '' : ' — not serving'}</option>`);
  // Aliases are what the platform resolves, so a name the registry does not know is
  // still valid: it may be an alias pointing at one of these.
  if (selected && !models.some((m) => m.name === selected)) {
    options.unshift(`<option value="${esc(selected)}" selected>${esc(selected)} (alias)</option>`);
  }
  if (!options.length) options.push('<option value="">— none registered —</option>');
  element.innerHTML = options.join('');
}

const setValue = (id, value) => { document.getElementById(id).value = value ?? ''; };
const setChecked = (id, value) => { document.getElementById(id).checked = Boolean(value); };

/* -------------------------------------------------------------------------- */
/* Users and roles (M03)                                                      */
/* -------------------------------------------------------------------------- */
let roleCache = [];

async function renderUsers() {
  const [page, roles] = await Promise.all([api('/users?limit=200'), api('/roles')]);
  roleCache = roles;

  document.getElementById('users-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>User</th><th>Signs in with</th><th>Roles</th>
          <th>Last seen</th><th>State</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${page.items.map(userRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary">${page.total} account(s).</div>`;

  document.getElementById('roles-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr><th>Role</th><th>Permissions</th></tr></thead>
        <tbody>${roles.map(roleRow).join('')}</tbody>
      </table>
    </div>`;

  document.querySelectorAll('[data-user-toggle]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const enable = button.dataset.userState !== 'true';
      await api(`/users/${button.dataset.userToggle}/active`, {
        method: 'PUT',
        body: JSON.stringify({ is_active: enable }),
      });
      alert(`${button.dataset.userName} ${enable ? 'enabled' : 'disabled'}.`, 'success');
      await renderUsers();
    });
  });

  document.querySelectorAll('[data-user-roles]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const current = (button.dataset.userCurrent || '').split(',').filter(Boolean);
      const wanted = await chooseRoles({
        userName: button.dataset.userName,
        provider: button.dataset.userProvider,
        current,
      });
      if (wanted === null) return;  // dismissed — distinct from clearing every role
      await api(`/users/${button.dataset.userRoles}/roles`, {
        method: 'PUT',
        body: JSON.stringify({ roles: wanted }),
      });
      alert(
        button.dataset.userProvider === 'local'
          ? `Roles updated for ${button.dataset.userName}.`
          : `Roles updated — but ${button.dataset.userName} signs in through ` +
            `${button.dataset.userProvider}, so the directory overwrites this at their ` +
            'next sign-in unless role sync is turned off.',
        button.dataset.userProvider === 'local' ? 'success' : 'warning');
      await renderUsers();
    });
  });

  document.querySelectorAll('[data-user-password]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const password = prompt(
        `Set a new password for ${button.dataset.userName}.\n\n` +
        'At least 12 characters. Their existing tokens stay valid until they expire — ' +
        'the platform has no revocation store yet.');
      if (!password) return;
      const result = await api(`/users/${button.dataset.userPassword}/password`, {
        method: 'PUT',
        body: JSON.stringify({ password }),
      });
      alert(result.message, 'success');
    });
  });
}

/**
 * Ask which roles a user should hold. Resolves to an array of role names — possibly
 * empty — or null if the dialog was dismissed.
 *
 * Checkboxes rather than the comma-separated `prompt()` this replaces: the browser box
 * could not show which roles exist, so an operator had to remember the names and type
 * them, and a typo produced a validation error rather than an assignment.
 *
 * Multi-select because that is what the platform actually models: `PUT /users/{id}/roles`
 * takes a list and permissions are the union of every role held. A single-choice control
 * would quietly drop the second role off anyone who has two.
 *
 * Clearing every box is a real choice, not a mistake, so it is allowed and spelled out —
 * it is how an account keeps its sign-in and loses everything it can do.
 */
function chooseRoles({ userName, provider, current }) {
  return new Promise((resolve) => {
    const element = document.getElementById('roles-modal');
    const modal = bootstrap.Modal.getOrCreateInstance(element);
    const note = document.getElementById('roles-note');
    document.getElementById('roles-user').textContent = userName;

    document.getElementById('roles-options').innerHTML = roleCache.map((role, index) => {
      const id = `role-choice-${index}`;
      return `<div class="form-check py-1">
        <input class="form-check-input" type="checkbox" name="role-choice" id="${id}"
               value="${esc(role.name)}" ${current.includes(role.name) ? 'checked' : ''}>
        <label class="form-check-label" for="${id}">
          <span class="fw-medium">${esc(role.name)}</span>
          <span class="small text-secondary ms-2"
                >${(role.permissions || []).length} permission(s)</span>
        </label>
      </div>`;
    }).join('') + `<div class="form-text small mt-2">
      Permissions are the union of every role ticked. Clear them all to leave the account
      able to sign in and do nothing.
    </div>`;

    // Only the caveat the operator cannot see for themselves. A federated account's roles
    // are overwritten at the next sign-in, so an assignment here can silently revert.
    const federated = provider && provider !== 'local';
    note.innerHTML = federated
      ? `${esc(userName)} signs in through ${esc(provider)}, so the directory overwrites ` +
        'this at their next sign-in unless role sync is turned off.'
      : '';
    note.classList.toggle('d-none', !federated);

    const form = document.getElementById('roles-form');
    // Assigned rather than added: this dialog is opened once per row, and addEventListener
    // would stack a handler per open and fire the previous rows' resolves too.
    form.onsubmit = (event) => {
      event.preventDefault();
      const picked = [...form.querySelectorAll('input[name="role-choice"]:checked')]
        .map((input) => input.value);
      form.onsubmit = null;
      element.removeEventListener('hidden.bs.modal', onDismiss);
      modal.hide();
      resolve(picked);
    };

    function onDismiss() {
      form.onsubmit = null;
      resolve(null);
    }
    element.addEventListener('hidden.bs.modal', onDismiss, { once: true });

    modal.show();
  });
}

function userRow(user) {
  const federated = user.auth_provider !== 'local';
  return `<tr class="${user.is_active ? '' : 'opacity-50'}">
    <td>
      <div class="fw-medium">${esc(user.username)}
        ${user.is_superuser ? '<span class="badge text-bg-danger ms-1">superuser</span>' : ''}
      </div>
      <div class="small text-secondary">${esc(user.full_name || '')} ${esc(user.email)}</div>
    </td>
    <td class="small">
      <span class="badge ${federated ? 'text-bg-info' : 'text-bg-secondary'}"
            >${esc(user.auth_provider)}</span>
    </td>
    <td class="small">
      ${user.roles.length
        ? user.roles.map((r) => `<span class="badge text-bg-secondary me-1">${esc(r.name)}</span>`).join('')
        : '<span class="text-secondary">none — can sign in, can do nothing</span>'}
    </td>
    <td class="small text-secondary">${user.last_login_at ? ago(user.last_login_at) : 'never'}</td>
    <td class="small">${user.is_active
      ? '<span class="text-success">active</span>'
      : '<span class="text-secondary">disabled</span>'}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-secondary" data-user-roles="${esc(user.id)}"
              data-user-name="${esc(user.username)}" data-user-provider="${esc(user.auth_provider)}"
              data-user-current="${esc(user.roles.map((r) => r.name).join(','))}">Roles</button>
      ${federated ? '' : `<button class="btn btn-sm btn-outline-secondary ms-1"
              data-user-password="${esc(user.id)}"
              data-user-name="${esc(user.username)}">Password</button>`}
      <button class="btn btn-sm btn-outline-${user.is_active ? 'danger' : 'success'} ms-1"
              data-user-toggle="${esc(user.id)}" data-user-state="${user.is_active}"
              data-user-name="${esc(user.username)}"
              >${user.is_active ? 'Disable' : 'Enable'}</button>
    </td>
  </tr>`;
}

function roleRow(role) {
  const names = (role.permissions || []).map((p) => p.name || p);
  return `<tr>
    <td class="fw-medium mono">${esc(role.name)}
      <div class="small text-secondary fw-normal">${esc(role.description || '')}</div>
    </td>
    <td class="small">
      ${names.length
        ? names.map((n) => `<span class="badge text-bg-light border me-1 mono">${esc(n)}</span>`).join('')
        : '<span class="text-secondary">none</span>'}
      <div class="small text-secondary mt-1">${names.length} permission(s)</div>
    </td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* Audit log (M24)                                                            */
/* -------------------------------------------------------------------------- */
async function renderAudit() {
  const query = new URLSearchParams({ limit: '100' });
  const action = document.getElementById('aud-action').value;
  const result = document.getElementById('aud-result').value;
  const resource = document.getElementById('aud-resource').value.trim();
  const since = document.getElementById('aud-since').value;
  if (action) query.set('action', action);
  if (result) query.set('result', result);
  if (resource) query.set('resource_type', resource);
  if (since) query.set('since', `${since}T00:00:00Z`);

  const [page, actions] = await Promise.all([
    api(`/audit?${query}`),
    api('/audit/actions'),
  ]);

  // Populated from what is actually in the log, not from the enum — forty filters that
  // all return nothing is worse than the eight that will not.
  const select = document.getElementById('aud-action');
  if (select.options.length !== actions.length + 1) {
    select.innerHTML = '<option value="">Any action</option>' +
      actions.map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
    select.value = action;
  }

  if (!page.items.length) {
    document.getElementById('audit-body').innerHTML = empty('Nothing matches those filters.');
    return;
  }

  document.getElementById('audit-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>When</th><th>Who</th><th>Action</th><th>Resource</th>
          <th>Result</th><th>From</th><th>Detail</th>
        </tr></thead>
        <tbody>${page.items.map(auditRow).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary">
      Showing ${page.items.length} of ${page.total} matching record(s).
      ${page.total > page.items.length
        ? 'Narrow the filters to see the rest — this list is truncated, not complete.'
        : 'That is all of them.'}
    </div>`;
}

function auditRow(row) {
  const tone = { SUCCESS: 'text-bg-success', FAILURE: 'text-bg-danger',
                 DENIED: 'text-bg-warning' }[row.result] || 'text-bg-secondary';
  const meta = row.metadata && Object.keys(row.metadata).length
    ? JSON.stringify(row.metadata) : '';
  return `<tr>
    <td class="small text-secondary" style="white-space:nowrap"
        title="${esc(row.timestamp)}">${esc(row.timestamp.replace('T', ' ').slice(0, 19))}</td>
    <td class="small mono">${esc(row.username)}</td>
    <td class="small mono">${esc(row.action)}</td>
    <td class="small text-secondary">
      ${row.resource_type ? esc(row.resource_type) : '—'}
      ${row.resource_id ? `<div class="mono" style="font-size:.75rem">${esc(String(row.resource_id).slice(0, 18))}</div>` : ''}
    </td>
    <td><span class="badge ${tone}">${esc(row.result)}</span></td>
    <td class="small text-secondary mono">${esc(row.source_ip || '—')}</td>
    <td class="small text-secondary" style="max-width:22rem">
      <div class="text-truncate" title="${esc(row.message || '')} ${esc(meta)}"
        >${esc(row.message || '')}${meta ? ` ${esc(meta)}` : ''}</div>
    </td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* Knowledge bases (M15)                                                      */
/* -------------------------------------------------------------------------- */

/* Which base the detail pane is showing. Held here rather than read back from the DOM
 * so the 5-second poll — which replaces innerHTML — does not collapse the pane an
 * operator has open while a document is still ingesting. */
let selectedBaseId = null;
/* The last retrieval preview, for the same reason: results are the answer to a question
 * someone asked, and a background refresh must not silently erase them. */
let lastSearch = null;

async function renderKnowledge() {
  const bases = await api('/knowledge-bases');

  if (!bases.length) {
    document.getElementById('kb-body').innerHTML = empty(
      'No knowledge bases. Create one to give agents documents to answer from — an ' +
      'agent with a knowledge base cites what it found, instead of recalling it.');
    document.getElementById('kb-detail').innerHTML = '';
    selectedBaseId = null;
    return;
  }

  if (!bases.some((b) => b.id === selectedBaseId)) selectedBaseId = bases[0].id;

  document.getElementById('kb-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Knowledge base</th><th>Tenant</th><th>Embedding model</th>
          <th>Chunking</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${bases.map(baseRow).join('')}</tbody>
      </table>
    </div>`;

  document.querySelectorAll('[data-kb-select]').forEach((el) => {
    el.onclick = (event) => {
      event.preventDefault();
      selectedBaseId = el.dataset.kbSelect;
      lastSearch = null;
      refresh({ force: true });
    };
  });

  document.querySelectorAll('[data-kb-delete]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Delete ${button.dataset.kbName}?\n\n` +
        'Its documents, their stored originals and its vector collection all go with it. ' +
        'Agents pointed at it will retrieve nothing.')) return;
      const result = await api(`/knowledge-bases/${button.dataset.kbDelete}`, { method: 'DELETE' });
      selectedBaseId = null;
      alert(result.message, 'success');
      await renderKnowledge();
    });
  });

  await renderBaseDetail(bases.find((b) => b.id === selectedBaseId));
}

function baseRow(base) {
  const selected = base.id === selectedBaseId;
  return `<tr class="${selected ? 'table-active' : ''}">
    <td>
      <a href="#knowledge" data-kb-select="${esc(base.id)}" class="fw-medium text-decoration-none"
         >${esc(base.display_name)}</a>
      <div class="small text-secondary mono">${esc(base.name)}</div>
      ${base.description ? `<div class="small text-secondary">${esc(base.description)}</div>` : ''}
    </td>
    <td class="small mono">${esc(base.tenant_id)}</td>
    <td class="small">
      <span class="mono">${esc(base.embedding_model)}</span>
      <div class="small text-secondary">${esc(base.embedding_dimensions)} dimensions</div>
    </td>
    <td class="small text-secondary">${esc(base.chunk_size)} / ${esc(base.chunk_overlap)} overlap</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-danger" data-kb-delete="${esc(base.id)}"
              data-kb-name="${esc(base.name)}">Delete</button>
    </td>
  </tr>`;
}

async function renderBaseDetail(base) {
  if (!base) { document.getElementById('kb-detail').innerHTML = ''; return; }

  const [detail, documents] = await Promise.all([
    api(`/knowledge-bases/${base.id}`),
    api(`/knowledge-bases/${base.id}/documents`),
  ]);

  document.getElementById('kb-detail').innerHTML = `
    <h6 class="mb-2 text-secondary">${esc(base.display_name)} — documents</h6>

    <div class="row g-2 mb-3">
      ${statCard('Documents', detail.documents)}
      ${statCard('Indexed', detail.indexed, detail.indexed ? 'text-success' : '')}
      ${statCard('Pending', detail.pending, detail.pending ? 'text-warning' : '')}
      ${statCard('Failed', detail.failed, detail.failed ? 'text-danger' : '')}
      ${statCard('Chunks', detail.chunks)}
    </div>

    <div class="card card-body py-2 mb-3">
      <div class="d-flex align-items-center gap-2 flex-wrap">
        <input type="file" id="kb-upload" class="form-control form-control-sm"
               style="max-width:24rem"
               accept=".pdf,.docx,.pptx,.txt,.md,.csv,.html,.json">
        <button id="kb-upload-btn" class="btn btn-sm btn-primary">Upload</button>
        <span class="small text-secondary">
          Parsing and embedding run in a worker, so this returns immediately and the row
          below walks PENDING → PARSING → CHUNKING → EMBEDDING → INDEXED.
        </span>
      </div>
    </div>

    ${documents.length ? `
      <div class="table-responsive mb-4">
        <table class="table table-sm align-middle">
          <thead><tr>
            <th>Document</th><th>Type</th><th>Size</th><th>Status</th>
            <th>Chunks</th><th>Uploaded</th><th class="text-end">Actions</th>
          </tr></thead>
          <tbody>${documents.map(documentRow).join('')}</tbody>
        </table>
      </div>` : empty('No documents yet. Upload one above.')}

    <h6 class="mb-2 text-secondary">Retrieval preview</h6>
    <p class="small text-secondary">
      Exactly what an agent pointed at this base would be given for a question — the first
      thing to check when an agent answers badly, and it answers without running one.
    </p>
    <div class="input-group input-group-sm mb-3" style="max-width:44rem">
      <input class="form-control" id="kb-query" placeholder="How many leave days carry over?"
             value="${esc(lastSearch?.query || '')}">
      <button class="btn btn-outline-secondary" id="kb-search">Search</button>
    </div>
    <div id="kb-results">${lastSearch ? searchResultsHtml(lastSearch) : ''}</div>`;

  document.getElementById('kb-upload-btn').onclick = (event) =>
    guard(event.target, async () => {
      const input = document.getElementById('kb-upload');
      const file = input.files?.[0];
      if (!file) { alert('Choose a file first.', 'warning'); return; }
      const accepted = await apiUpload(`/knowledge-bases/${base.id}/documents`, file);
      input.value = '';
      alert(`${accepted.filename} accepted (${accepted.status}). ${accepted.message}`, 'success');
      await refresh({ force: true });
    });

  const runSearch = (event) => guard(event.target, async () => {
    const query = document.getElementById('kb-query').value.trim();
    if (!query) return;
    const response = await api(`/knowledge-bases/${base.id}/search`, {
      method: 'POST',
      body: JSON.stringify({ query, limit: 5 }),
    });
    lastSearch = response;
    document.getElementById('kb-results').innerHTML = searchResultsHtml(response);
  });
  document.getElementById('kb-search').onclick = runSearch;
  document.getElementById('kb-query').onkeydown = (e) => { if (e.key === 'Enter') runSearch(e); };

  document.querySelectorAll('[data-doc-delete]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Delete ${button.dataset.docName}?`)) return;
      const result = await api(`/documents/${button.dataset.docDelete}`, { method: 'DELETE' });
      alert(result.message, 'success');
      await refresh({ force: true });
    });
  });
}

const statCard = (label, value, tone = '') => `
  <div class="col-auto">
    <div class="card card-body py-2 px-3">
      <div class="small text-secondary">${esc(label)}</div>
      <div class="fs-5 ${tone}">${esc(value)}</div>
    </div>
  </div>`;

function documentRow(doc) {
  const terminal = { INDEXED: 'text-bg-success', FAILED: 'text-bg-danger',
                     NO_TEXT: 'text-bg-warning' };
  // A NO_TEXT document is usually a scan awaiting OCR (Phase 9), not a broken file, and
  // status_detail is where that distinction lives — surfaced here so it is not mistaken
  // for a failure.
  const note = doc.error || doc.status_detail;
  return `<tr>
    <td>
      <div class="fw-medium">${esc(doc.filename)}</div>
      ${note ? `<div class="small text-danger">${esc(note)}</div>` : ''}
      ${doc.ocr_used ? '<div class="small text-secondary">text recovered by OCR</div>' : ''}
    </td>
    <td class="small text-secondary mono">${esc(doc.content_type || '—')}</td>
    <td class="small">${(doc.size_bytes / 1024).toFixed(0)} KiB</td>
    <td><span class="badge ${terminal[doc.status] || 'text-bg-secondary'}"
              >${esc(doc.status)}</span></td>
    <td class="small">${esc(doc.chunk_count)}</td>
    <td class="small text-secondary">${ago(doc.created_at)}</td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-danger" data-doc-delete="${esc(doc.id)}"
              data-doc-name="${esc(doc.filename)}">Delete</button>
    </td>
  </tr>`;
}

function searchResultsHtml(response) {
  if (!response.results.length) {
    // Distinguished from an error deliberately: "nothing matched" and "the search broke"
    // send an operator to entirely different places.
    return `<div class="alert alert-warning py-2 small">
      Nothing matched “${esc(response.query)}”. The documents may still be ingesting, or
      the wording may be too far from theirs — an agent asked this would answer from the
      model alone.</div>`;
  }
  return `<div class="small text-secondary mb-2">
      ${response.results.length} passage(s), best first. This is the text the agent sees.
    </div>` +
    response.results.map((r) => `
      <div class="card card-body py-2 mb-2">
        <div class="d-flex align-items-center mb-1">
          <span class="mono small">${esc(r.document_name)}</span>
          ${r.location ? `<span class="small text-secondary ms-2">${esc(r.location)}</span>` : ''}
          <span class="badge text-bg-secondary ms-auto">score ${r.score.toFixed(3)}</span>
        </div>
        <div class="small" style="white-space:pre-wrap">${esc(r.text)}</div>
      </div>`).join('');
}

/* -------------------------------------------------------------------------- */
/* Memory (M16)                                                               */
/* -------------------------------------------------------------------------- */
async function renderMemory() {
  const tenant = document.getElementById('mem-tenant').value.trim() || 'default';
  const subject = document.getElementById('mem-user').value.trim();

  const query = new URLSearchParams({ tenant_id: tenant, limit: '100' });
  if (subject) query.set('end_user', subject);
  const entries = await api(`/memory/entries?${query}`);

  if (!entries.length) {
    document.getElementById('memory-body').innerHTML = empty(
      subject
        ? `Nothing remembered about ${subject} in ${tenant}.`
        : `No memories in ${tenant}. Agents do not write memory yet — see docs/rag.md.`);
    return;
  }

  document.getElementById('memory-body').innerHTML = `
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr>
          <th>Remembered</th><th>Layer</th><th>Kind</th><th>Importance</th>
          <th>Subject</th><th>Expires</th><th class="text-end">Actions</th>
        </tr></thead>
        <tbody>${entries.map((e) => memoryRow(e, tenant)).join('')}</tbody>
      </table>
    </div>
    <div class="small text-secondary mt-2">
      An expired memory is skipped on recall but still stored — nothing sweeps them yet.
    </div>`;

  document.querySelectorAll('[data-mem-forget]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      const result = await api(
        `/memory/entries/${button.dataset.memForget}?tenant_id=${encodeURIComponent(tenant)}`,
        { method: 'DELETE' });
      alert(result.message, 'success');
      await renderMemory();
    });
  });
}

function memoryRow(entry, tenant) {
  const expired = entry.expires_at && new Date(entry.expires_at) < new Date();
  return `<tr class="${expired ? 'opacity-50' : ''}">
    <td class="small" style="max-width:32rem">${esc(entry.text)}</td>
    <td class="small"><span class="badge text-bg-secondary">${esc(entry.layer)}</span></td>
    <td class="small text-secondary">${esc(entry.kind)}</td>
    <td class="small">${Number(entry.importance).toFixed(2)}</td>
    <td class="small mono text-secondary">${esc(entry.end_user || '—')}</td>
    <td class="small text-secondary">
      ${entry.expires_at ? `${expired ? 'expired ' : ''}${ago(entry.expires_at)}` : 'never'}
    </td>
    <td class="text-end">
      <button class="btn btn-sm btn-outline-danger" data-mem-forget="${esc(entry.id)}"
              data-mem-tenant="${esc(tenant)}">Forget</button>
    </td>
  </tr>`;
}

/* -------------------------------------------------------------------------- */
/* Navigation and polling                                                     */
/* -------------------------------------------------------------------------- */
const RENDERERS = {
  dashboard: renderDashboard,
  agents: renderAgents,
  skills: renderSkills,
  tools: renderTools,
  approvals: renderApprovals,
  chat: renderChat,
  nodes: renderNodes,
  gpus: renderGpus,
  containers: renderContainers,
  models: renderModels,
  deployments: renderDeployments,
  endpoints: renderEndpoints,
  knowledge: renderKnowledge,
  memory: renderMemory,
  voice: renderVoice,
  users: renderUsers,
  audit: renderAudit,
};

async function refresh({ force = false } = {}) {
  // A refresh replaces the page's innerHTML, which destroys whatever the operator is
  // currently interacting with — most visibly the alias repointing dropdown, which the
  // poll would close mid-selection every five seconds. Navigation and explicit actions
  // pass force; the timer does not.
  if (!force && document.activeElement?.closest('.page')) return;

  try {
    await RENDERERS[currentPage]();
    document.getElementById('poll-status').textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    if (!/Session expired/.test(err.message)) alert(err.message);
  }
}

function navigate(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach((s) => s.classList.add('d-none'));
  document.getElementById(`page-${page}`).classList.remove('d-none');
  document.querySelectorAll('.nav-item[data-page]').forEach((a) =>
    a.classList.toggle('active', a.dataset.page === page));
  document.getElementById('alert-slot').innerHTML = '';
  refresh({ force: true });
}

function startPolling() {
  stopPolling();
  // Refreshes on a timer rather than a socket: a 5s poll is well within what the
  // cached inventory endpoints cost, and it keeps the UI dependency-free.
  pollTimer = setInterval(refresh, POLL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

/* -------------------------------------------------------------------------- */
/* Session                                                                    */
/* -------------------------------------------------------------------------- */
function showLogin() {
  stopPolling();
  document.getElementById('login-view').classList.remove('d-none');
  document.getElementById('app-view').classList.add('d-none');
  document.getElementById('user-badge').classList.add('d-none');
  document.getElementById('logout-btn').classList.add('d-none');
  document.getElementById('poll-status').textContent = '';
}

async function showApp() {
  const me = await api('/auth/me');
  document.getElementById('login-view').classList.add('d-none');
  document.getElementById('app-view').classList.remove('d-none');

  const badge = document.getElementById('user-badge');
  badge.textContent = me.is_superuser ? `${me.username} · superuser` : me.username;
  badge.classList.remove('d-none');
  document.getElementById('logout-btn').classList.remove('d-none');

  navigate(location.hash.replace('#', '') || 'dashboard');
  startPolling();
}

/* -------------------------------------------------------------------------- */
/* Wiring                                                                     */
/* -------------------------------------------------------------------------- */
document.getElementById('login-form').onsubmit = async (event) => {
  event.preventDefault();
  const error = document.getElementById('login-error');
  error.classList.add('d-none');
  try {
    const result = await api('/auth/login', {
      method: 'POST',
      body: JSON.stringify({
        username: document.getElementById('username').value,
        password: document.getElementById('password').value,
      }),
    });
    token.set(result.access_token);
    await showApp();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('d-none');
  }
};

document.getElementById('logout-btn').onclick = async () => {
  try { await api('/auth/logout', { method: 'POST' }); } catch { /* audit-only */ }
  token.clear();
  showLogin();
};

document.getElementById('register-form').onsubmit = async (event) => {
  event.preventDefault();
  const error = document.getElementById('register-error');
  error.classList.add('d-none');
  try {
    const result = await api('/nodes', {
      method: 'POST',
      body: JSON.stringify({
        name: document.getElementById('reg-name').value,
        agent_url: document.getElementById('reg-url').value,
        agent_token: document.getElementById('reg-token').value,
        verify_tls: document.getElementById('reg-verify').checked,
      }),
    });
    bootstrap.Modal.getInstance(document.getElementById('register-modal')).hide();
    event.target.reset();
    alert(
      `Registered ${result.node.name}: ${result.sync.gpus_seen} GPUs, ` +
      `${result.sync.containers_seen} containers.`, 'success');
    await renderNodes();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('d-none');
  }
};

document.getElementById('managed-only').onchange = renderContainers;
document.getElementById('dash-window').onchange = () => refresh({ force: true });

document.getElementById('agent-modal').addEventListener('show.bs.modal', async () => {
  // Populated on open rather than kept in sync: an operator creating an agent needs the
  // tools that exist *now*, and a tool enabled a minute ago must be selectable.
  const [aliases, tools, skills] = await Promise.all([
    api('/model-aliases'), api('/tools'), api('/skills'),
  ]);
  toolCache = tools;
  skillCache = skills;

  document.getElementById('agt-model').innerHTML = aliases
    .filter((a) => a.enabled)
    .map((a) => `<option value="${esc(a.alias)}" ${a.serving ? '' : 'disabled'}>` +
      `${esc(a.alias)}${a.serving ? '' : ' (not serving)'}</option>`).join('');

  // Disabled tools are shown but not selectable: granting one would look like it worked
  // and then never be offered to the model.
  document.getElementById('agt-tools').innerHTML = tools.map((tool) =>
    `<option value="${esc(tool.id)}" ${tool.enabled ? '' : 'disabled'}>` +
    `${esc(tool.name)} — ${esc(tool.risk_level)}${tool.enabled ? '' : ' (disabled)'}</option>`).join('');

  document.getElementById('agt-skills').innerHTML = skills.map((skill) =>
    `<option value="${esc(skill.id)}">${esc(skill.name)}</option>`).join('');
});

const selectedValues = (id) =>
  Array.from(document.getElementById(id).selectedOptions).map((o) => o.value);

onModalSubmit('agent-form', 'agent-error', async (form) => {
  const agent = await api('/agents', {
    method: 'POST',
    body: JSON.stringify({
      slug: field('agt-slug'),
      display_name: field('agt-name'),
      description: field('agt-desc') || null,
      system_prompt: field('agt-prompt'),
      model: field('agt-model'),
      temperature: numberField('agt-temp') ?? 0.2,
      max_iterations: numberField('agt-iter') ?? 10,
      tool_ids: selectedValues('agt-tools'),
      skill_ids: selectedValues('agt-skills'),
    }),
  });
  closeModal('agent-modal');
  form.reset();
  alert(`Created ${agent.slug}. It is now selectable in chat as agent:${agent.slug}.`, 'success');
  await renderAgents();
});

onModalSubmit('skill-form', 'skill-error', async (form) => {
  const skill = await api('/skills', {
    method: 'POST',
    body: JSON.stringify({
      name: field('skl-name'),
      display_name: field('skl-display'),
      description: field('skl-desc'),
      instructions: field('skl-instructions'),
      version: field('skl-version') || '1.0',
      required_tools: field('skl-tools')
        ? field('skl-tools').split(',').map((s) => s.trim()).filter(Boolean)
        : [],
      required_permission: field('skl-perm') || null,
    }),
  });
  closeModal('skill-modal');
  form.reset();
  document.getElementById('skl-version').value = '1.0';
  alert(`Created ${skill.name}. It is now selectable when creating or editing an agent.`,
    'success');
  await renderSkills();
});

onModalSubmit('mcp-form', 'mcp-error', async (form) => {
  const server = await api('/mcp/servers', {
    method: 'POST',
    body: JSON.stringify({
      name: field('mcp-name'),
      endpoint: field('mcp-endpoint'),
      description: field('mcp-desc') || null,
      credentials: field('mcp-cred') || null,
    }),
  });
  closeModal('mcp-modal');
  form.reset();
  alert(`Registered ${server.name}. Run Discover to catalogue its tools — they arrive ` +
    'disabled until you review them.', 'success');
  await renderTools();
});

/** Submit a modal form, showing failures inline rather than behind the modal. */
function onModalSubmit(formId, errorId, handler) {
  const form = document.getElementById(formId);
  form.onsubmit = async (event) => {
    event.preventDefault();
    const error = document.getElementById(errorId);
    error.classList.add('d-none');
    try {
      await handler(form);
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove('d-none');
    }
  };
}

const field = (id) => document.getElementById(id).value.trim();
const numberField = (id) => (field(id) === '' ? null : Number(field(id)));
const closeModal = (id) => bootstrap.Modal.getInstance(document.getElementById(id))?.hide();

/** Runtimes the platform points at rather than starts (M07, M26).
 *
 * They need a URL and no storage path, and the platform reserves no GPUs for them —
 * which is how a real Whisper or Fish Speech deployment is registered without the
 * platform trying to schedule a container for it.
 */
const EXTERNAL_RUNTIMES = new Set(['external', 'ollama']);

/** Show the field that applies and hide the one that does not.
 *
 * Both visible at once invites filling in both, and the platform then has a model with a
 * storage path it will never read next to a URL it will never call.
 */
function syncModelRuntimeFields() {
  const external = EXTERNAL_RUNTIMES.has(document.getElementById('mdl-runtime').value);
  document.getElementById('mdl-endpoint-row').classList.toggle('d-none', !external);
  document.getElementById('mdl-path-row').classList.toggle('d-none', external);
  document.getElementById('mdl-endpoint').required = external;
  document.getElementById('mdl-path').required = !external;
  // An external model reserves nothing: the GPUs it uses belong to whoever started it,
  // and letting the scheduler count them would double-book the card.
  const gpus = document.getElementById('mdl-gpus');
  if (external) gpus.value = '0';
}

document.getElementById('mdl-runtime').onchange = syncModelRuntimeFields;
syncModelRuntimeFields();

onModalSubmit('model-form', 'model-error', async (form) => {
  const runtime = field('mdl-runtime');
  const external = EXTERNAL_RUNTIMES.has(runtime);
  const model = await api('/models', {
    method: 'POST',
    body: JSON.stringify({
      name: field('mdl-name'),
      display_name: field('mdl-display'),
      type: field('mdl-type'),
      runtime,
      // One or the other, never both — see syncModelRuntimeFields. The platform
      // requires a storage path for a runtime it starts and an endpoint for one it
      // does not, and rejects the wrong combination.
      // Empty for an external model rather than a made-up path: it has no local files,
      // and inventing one would put a path in the registry that nothing will ever read.
      storage_path: external ? '' : field('mdl-path'),
      endpoint_url: external ? field('mdl-endpoint') : null,
      context_length: numberField('mdl-context'),
      required_gpu_memory_mib: external ? 0 : numberField('mdl-mem'),
      min_gpu_count: external ? 0 : (numberField('mdl-gpus') ?? 1),
    }),
  });
  closeModal('model-modal');
  form.reset();
  // Registration writes metadata only — it does not touch the filesystem, so the model
  // is REGISTERED, not AVAILABLE, until Verify confirms the weights are actually there.
  alert(`Registered ${model.name} (${model.status}). Run Verify to catalogue its files.`,
    'success');
  await renderModels();
});

onModalSubmit('deploy-form', 'deploy-error', async (form) => {
  const indices = field('dep-gpus');
  const accepted = await api(`/models/${form.dataset.modelId}/deploy`, {
    method: 'POST',
    body: JSON.stringify({
      node_id: field('dep-node') || null,
      gpu_ids: indices ? indices.split(',').map((n) => Number(n.trim())) : null,
      gpu_memory_utilization: numberField('dep-util'),
    }),
  });
  closeModal('deploy-modal');
  form.reset();
  alert(`${accepted.message} Watch it under Deployments.`, 'success');
  navigate('deployments');
});

onModalSubmit('alias-form', 'alias-error', async (form) => {
  await api('/model-aliases', {
    method: 'POST',
    body: JSON.stringify({ alias: field('alias-name'), model_id: field('alias-model') }),
  });
  closeModal('alias-modal');
  form.reset();
  alert('Alias created.', 'success');
  await renderEndpoints();
});

document.getElementById('alias-modal').addEventListener('show.bs.modal', () => {
  document.getElementById('alias-model').innerHTML =
    modelCache.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join('');
});

onModalSubmit('add-node-form', 'add-node-error', async () => {
  const created = await api('/node-enrollments', {
    method: 'POST',
    body: JSON.stringify({
      name: field('add-node-name'),
      description: field('add-node-desc') || null,
    }),
  });
  // Revealed in place with the modal left open, exactly as an API key is: this is the
  // only moment the token exists anywhere but the operator's clipboard.
  document.getElementById('add-node-command').textContent = created.command;
  document.getElementById('add-node-expiry').textContent =
    `${Math.round((new Date(created.expires_at) - Date.now()) / 60000)} minutes`;
  document.getElementById('add-node-result').classList.remove('d-none');
  document.getElementById('add-node-submit').disabled = true;
  await renderNodes();
});

document.getElementById('add-node-copy').onclick = async (event) => {
  await navigator.clipboard.writeText(document.getElementById('add-node-command').textContent);
  event.target.textContent = 'Copied';
  setTimeout(() => { event.target.textContent = 'Copy command'; }, 2000);
};

document.getElementById('add-node-modal').addEventListener('hidden.bs.modal', () => {
  document.getElementById('add-node-form').reset();
  document.getElementById('add-node-result').classList.add('d-none');
  // Cleared on close so the token does not sit in the DOM for the rest of the session.
  document.getElementById('add-node-command').textContent = '';
  document.getElementById('add-node-submit').disabled = false;
});

onModalSubmit('key-form', 'key-error', async () => {
  const created = await api('/api-keys', {
    method: 'POST',
    body: JSON.stringify({
      client_id: field('key-client'),
      name: field('key-name'),
      rate_limit_per_minute: numberField('key-rate') ?? 120,
    }),
  });
  // Shown in place rather than in a toast, and the modal stays open: this is the only
  // time the key exists anywhere outside the caller's clipboard.
  document.getElementById('key-secret').textContent = created.api_key;
  document.getElementById('key-result').classList.remove('d-none');
  document.getElementById('key-submit').disabled = true;
  await renderEndpoints();
});

document.getElementById('key-modal').addEventListener('hidden.bs.modal', () => {
  document.getElementById('key-form').reset();
  document.getElementById('key-result').classList.add('d-none');
  document.getElementById('key-secret').textContent = '';
  document.getElementById('key-submit').disabled = false;
});

document.getElementById('new-client').onclick = async () => {
  const name = prompt('Client name (the application this key belongs to):');
  if (!name) return;
  try {
    const created = await api('/api-clients', {
      method: 'POST', body: JSON.stringify({ name }),
    });
    const select = document.getElementById('key-client');
    select.insertAdjacentHTML('beforeend',
      `<option value="${esc(created.id)}">${esc(created.name)}</option>`);
    select.value = created.id;
  } catch (err) {
    alert(err.message);
  }
};

document.getElementById('mcp-import').onclick = (event) => guard(event.target, async () => {
  const results = await api('/mcp/servers/import-manifests', { method: 'POST' });
  if (!results.length) {
    alert('No MCP manifests found in mcp/manifests/.', 'warning');
    return;
  }
  const created = results.reduce((sum, r) => sum + r.created, 0);
  const failed = results.filter((r) => (r.detail || '').startsWith('Discovery failed'));
  alert(
    `${results.length} server(s): ${created} new tool(s) catalogued, disabled at HIGH risk ` +
    `until reviewed.` +
    (failed.length ? ` ${failed.length} unreachable — is its container running?` : ''),
    failed.length ? 'warning' : 'success');
  await renderTools();
});

document.getElementById('import-ollama').onclick = (event) => guard(event.target, async () => {
  const results = await api('/models/import-ollama', { method: 'POST' });
  if (!results.length) {
    alert('Ollama is reachable but has no models. Pull one first — e.g. `ollama pull ' +
      'llama3.2` — then try again.', 'warning');
    return;
  }
  const fresh = results.filter((r) => r.status === 'registered').length;
  alert(
    `${results.length} model(s) from Ollama, ${fresh} new. They are AVAILABLE straight ` +
    'away — Ollama already holds the weights. Deploy one to attach, then alias it.',
    'success');
  await renderModels();
});

document.getElementById('import-manifests').onclick = (event) => guard(event.target, async () => {
  const results = await api('/models/import-manifests', { method: 'POST' });
  if (!results.length) {
    alert('No manifests found in models/manifests/.', 'warning');
    return;
  }
  const ready = results.filter((r) => r.status === 'AVAILABLE').length;
  alert(`${results.length} manifest(s) processed, ${ready} model(s) AVAILABLE.`,
    ready ? 'success' : 'warning');
  await renderModels();
});

/* -------------------------------------------------------------------------- */
/* Knowledge and memory wiring                                                */
/* -------------------------------------------------------------------------- */
document.getElementById('kb-modal').addEventListener('show.bs.modal', async () => {
  const [aliases, models] = await Promise.all([api('/model-aliases'), api('/models')]);
  const embeddingIds = new Set(
    (models.items || []).filter((m) => m.type === 'EMBEDDING').map((m) => m.id));
  // Only EMBEDDING aliases are offered. A chat model produces something vector-shaped and
  // the collection is created happily; the failure surfaces later as retrieval that finds
  // nothing, which is a long way from this form.
  const usable = aliases.filter((a) => a.enabled && embeddingIds.has(a.model_id));

  const select = document.getElementById('kb-embed');
  select.innerHTML = usable.length
    ? usable.map((a) => `<option value="${esc(a.alias)}" ${a.serving ? '' : 'disabled'}>` +
        `${esc(a.alias)}${a.serving ? '' : ' (not serving)'}</option>`).join('')
    : '<option value="" disabled selected>No embedding model is serving</option>';

  const error = document.getElementById('kb-error');
  if (!usable.some((a) => a.serving)) {
    error.textContent =
      'No alias points at a serving EMBEDDING model. Deploy one and alias it first — the ' +
      'vector width is discovered by embedding a probe string, so the model has to answer ' +
      'before a base can exist.';
    error.classList.remove('d-none');
  } else {
    error.classList.add('d-none');
  }
});

onModalSubmit('kb-form', 'kb-error', async (form) => {
  const base = await api('/knowledge-bases', {
    method: 'POST',
    body: JSON.stringify({
      name: field('kb-name'),
      display_name: field('kb-display'),
      description: field('kb-desc') || null,
      embedding_model: field('kb-embed'),
      tenant_id: field('kb-tenant') || 'default',
      chunk_size: numberField('kb-chunk') ?? 1200,
      chunk_overlap: numberField('kb-overlap') ?? 150,
    }),
  });
  closeModal('kb-modal');
  form.reset();
  document.getElementById('kb-tenant').value = 'default';
  document.getElementById('kb-chunk').value = 1200;
  document.getElementById('kb-overlap').value = 150;
  selectedBaseId = base.id;
  alert(`Created ${base.name} with ${base.embedding_dimensions}-dimension vectors. ` +
    'Upload documents, then add it to an agent.', 'success');
  await renderKnowledge();
});

document.getElementById('mem-load').onclick = (event) =>
  guard(event.target, () => refresh({ force: true }));
document.getElementById('mem-user').onkeydown = (e) => {
  if (e.key === 'Enter') { e.preventDefault(); refresh({ force: true }); }
};

document.getElementById('mem-forget-all').onclick = (event) =>
  guard(event.target, async () => {
    const tenant = document.getElementById('mem-tenant').value.trim() || 'default';
    const subject = document.getElementById('mem-user').value.trim();
    // Required, not optional: without a subject this would erase the tenant's memory of
    // everyone, and the endpoint would accept it.
    if (!subject) {
      alert('Enter the subject to erase. Erasing a whole tenant is not offered here.',
        'warning');
      return;
    }
    if (!confirm(`Erase everything remembered about ${subject} in ${tenant}?\n\n` +
      'All three layers, and the session state. This cannot be undone.')) return;
    const result = await api('/memory/forget-all', {
      method: 'POST',
      body: JSON.stringify({ tenant_id: tenant, end_user: subject, query: '' }),
    });
    alert(result.message, 'success');
    await renderMemory();
  });

document.getElementById('user-modal').addEventListener('show.bs.modal', async () => {
  const roles = await api('/roles');
  roleCache = roles;
  document.getElementById('usr-roles').innerHTML = roles
    .map((r) => `<option value="${esc(r.name)}">${esc(r.name)} — ` +
      `${(r.permissions || []).length} permission(s)</option>`).join('');
});

onModalSubmit('user-form', 'user-error', async (form) => {
  const user = await api('/users', {
    method: 'POST',
    body: JSON.stringify({
      username: field('usr-name'),
      email: field('usr-email'),
      password: document.getElementById('usr-pass').value,
      full_name: field('usr-full') || null,
      roles: selectedValues('usr-roles'),
    }),
  });
  closeModal('user-modal');
  form.reset();
  alert(`Created ${user.username} with ${user.roles.length} role(s).`, 'success');
  await renderUsers();
});

document.getElementById('aud-apply').onclick = (event) =>
  guard(event.target, () => refresh({ force: true }));
document.getElementById('aud-clear').onclick = (event) => guard(event.target, () => {
  document.getElementById('aud-action').value = '';
  document.getElementById('aud-result').value = '';
  document.getElementById('aud-resource').value = '';
  document.getElementById('aud-since').value = '';
  return refresh({ force: true });
});

document.querySelectorAll('.nav-item[data-page]').forEach((link) => {
  link.onclick = (event) => { event.preventDefault(); navigate(link.dataset.page); };
});

// Pause polling while the tab is hidden. A console left open on a wall display
// should not keep a request in flight every 5 seconds indefinitely.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else if (token.get()) { refresh({ force: true }); startPolling(); }
});

if (token.get()) {
  showApp().catch(showLogin);
} else {
  showLogin();
}


onModalSubmit('voice-config-form', 'voice-error', async () => {
  // Sent whole rather than as a diff: the form shows every field, so what is on screen
  // is what the operator means. A partial update from a full form is how a setting
  // somebody deliberately cleared comes back.
  const config = await api('/voice/config', {
    method: 'PUT',
    body: JSON.stringify({
      enabled: document.getElementById('vc-enabled').checked,
      stt_model: field('vc-stt'),
      tts_model: field('vc-tts'),
      default_agent_slug: field('vc-agent'),
      default_language: field('vc-language'),
      default_voice: field('vc-voice'),
      sample_rate_hz: numberField('vc-rate'),
      max_session_seconds: numberField('vc-max'),
      idle_timeout_seconds: numberField('vc-idle'),
      interrupt_enabled: document.getElementById('vc-interrupt').checked,
      vad_enabled: document.getElementById('vc-vad').checked,
      store_audio: document.getElementById('vc-store-audio').checked,
      store_transcripts: document.getElementById('vc-store-transcripts').checked,
      retention_days: numberField('vc-retention'),
    }),
  });
  alert(
    config.store_audio
      ? 'Saved. Audio recording is ON — raw speech will be retained.'
      : 'Saved. Applies to the next session.',
    config.store_audio ? 'warning' : 'success');
  await renderVoice();
});

/* -------------------------------------------------------------------------- */
/* Colour theme                                                               */
/* -------------------------------------------------------------------------- */
/* Dark is the default and stays the default: this is watched for hours at a time, and
   the status palette was chosen against a dark surface. The choice is remembered per
   browser rather than per user — it is a property of the screen someone is sitting at,
   not of the account, and the same operator on a wall display and a laptop wants
   different answers. */
(() => {
  const KEY = 'ai-platform-theme';
  const root = document.documentElement;
  const button = document.getElementById('theme-toggle');
  if (!button) return;

  const MOON = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" stroke="currentColor" ' +
               'stroke-width="1.6" stroke-linejoin="round"/>';
  const SUN = '<circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.6"/>' +
              '<path d="M12 2.6v2.2M12 19.2v2.2M4.2 12H2m20 0h-2.2M6.3 6.3 4.8 4.8m14.4 14.4' +
              '-1.5-1.5M6.3 17.7l-1.5 1.5M19.2 4.8l-1.5 1.5" stroke="currentColor" ' +
              'stroke-width="1.6" stroke-linecap="round"/>';

  const apply = (theme) => {
    root.setAttribute('data-bs-theme', theme);
    // The icon shows what you would switch *to*, which is the convention people expect;
    // showing the current state instead reads as a status light nobody can act on.
    document.getElementById('theme-icon').innerHTML = theme === 'dark' ? SUN : MOON;
  };

  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch {
    // Private browsing, or storage disabled by policy. Not worth failing over: the
    // default is still correct, the choice simply will not survive a reload.
  }
  apply(stored === 'light' ? 'light' : 'dark');

  button.addEventListener('click', () => {
    const next = root.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    apply(next);
    try {
      localStorage.setItem(KEY, next);
    } catch { /* see above */ }
  });
})();
