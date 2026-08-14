/* Developer portal — self-service API keys (M20).
 *
 * Same no-build-step constraint as the admin console (§4.3): an air-gapped install is a
 * file copy, and a bundler would be a toolchain to vendor and reproduce on the target for
 * no benefit at this size.
 *
 * Scoped narrowly on purpose. A developer needs a credential, a base URL and a snippet
 * that works; everything an operator needs is absent, because showing a control someone
 * cannot use reads as a permissions bug rather than as a boundary.
 */
'use strict';

const API = '/api/v1';
const TOKEN_KEY = 'aip.dev.token';

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

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function alert(message, kind = 'danger') {
  const slot = document.getElementById('alert-slot');
  slot.innerHTML = `<div class="alert alert-${kind} alert-dismissible py-2 small">` +
    `${esc(message)}<button class="btn-close btn-sm" data-bs-dismiss="alert"></button></div>`;
  if (kind === 'success') setTimeout(() => (slot.innerHTML = ''), 6000);
}

const empty = (text) => `<div class="text-secondary small py-3">${esc(text)}</div>`;

function ago(iso) {
  if (!iso) return 'never';
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/* -------------------------------------------------------------------------- */
/* Rendering                                                                  */
/* -------------------------------------------------------------------------- */
let aliasCache = [];

const baseUrl = () => `${location.origin}/v1`;

async function render() {
  document.getElementById('base-url').textContent = baseUrl();

  const [aliases, keys] = await Promise.all([
    api('/model-aliases').catch(() => []),
    api('/api-keys').catch(() => []),
  ]);
  aliasCache = aliases.filter((a) => a.enabled);

  document.getElementById('models-body').innerHTML = aliasCache.length ? `
    <div class="table-responsive"><table class="table table-sm align-middle">
      <thead><tr><th>Model</th><th>Description</th><th>Status</th></tr></thead>
      <tbody>${aliasCache.map((a) => `<tr>
        <td class="mono">${esc(a.alias)}</td>
        <td class="small text-secondary">${esc(a.description || '')}</td>
        <td class="small">${a.serving
          ? '<span class="text-success">serving</span>'
          // Shown rather than hidden: a model that exists but is not serving is a
          // question for an operator, and hiding it turns that into "the model is gone".
          : '<span class="text-secondary">not serving right now</span>'}</td>
      </tr>`).join('')}</tbody>
    </table></div>` : empty('No models are published yet. Ask an operator to publish an alias.');

  document.getElementById('keys-body').innerHTML = keys.length ? `
    <div class="table-responsive"><table class="table table-sm align-middle">
      <thead><tr>
        <th>Name</th><th>Key</th><th>Scopes</th><th>Limit</th>
        <th>Last used</th><th>Expires</th><th class="text-end">Actions</th>
      </tr></thead>
      <tbody>${keys.map(keyRow).join('')}</tbody>
    </table></div>` : empty('No keys yet. Create one to start calling the API.');

  wireKeyActions();
  renderSnippets();
  await renderUsage();
}

function keyRow(key) {
  const expired = key.expires_at && new Date(key.expires_at) < new Date();
  const dead = key.revoked_at || expired;
  return `<tr class="${dead ? 'opacity-50' : ''}">
    <td>
      <div class="fw-medium">${esc(key.name)}</div>
      ${key.rotated_to
        // Named explicitly: seeing traffic on a key that should be gone is the difference
        // between "still rotating" and "someone is using a credential we retired".
        ? '<div class="small text-warning">rotated — this one stops working at the date shown</div>'
        : ''}
    </td>
    <td class="mono small">${esc(key.prefix)}…</td>
    <td class="small">
      ${(key.scopes || []).length
        ? key.scopes.map((s) => `<span class="badge text-bg-secondary me-1 mono">${esc(s)}</span>`).join('')
        : '<span class="text-secondary">unrestricted</span>'}
    </td>
    <td class="small text-secondary">${esc(key.rate_limit_per_minute)}/min</td>
    <td class="small text-secondary">${key.last_used_at ? ago(key.last_used_at) : 'never'}</td>
    <td class="small text-secondary">
      ${key.revoked_at ? 'revoked'
        : key.expires_at ? `${expired ? 'expired ' : ''}${new Date(key.expires_at).toISOString().slice(0, 10)}`
        : 'never'}
    </td>
    <td class="text-end">
      ${dead ? '' : `
        <button class="btn btn-sm btn-outline-secondary" data-rotate="${esc(key.id)}"
                data-name="${esc(key.name)}">Rotate</button>
        <button class="btn btn-sm btn-outline-danger ms-1" data-revoke="${esc(key.id)}"
                data-name="${esc(key.name)}">Revoke</button>`}
    </td>
  </tr>`;
}

function wireKeyActions() {
  document.querySelectorAll('[data-revoke]').forEach((button) => {
    button.onclick = async () => {
      if (!confirm(`Revoke "${button.dataset.name}"?\n\n` +
        'It stops working immediately. Anything still using it starts failing now — ' +
        'rotate instead if you need an overlap.')) return;
      try {
        const result = await api(`/api-keys/${button.dataset.revoke}`, { method: 'DELETE' });
        alert(result.message || 'Key revoked.', 'success');
        await render();
      } catch (err) { alert(err.message); }
    };
  });

  document.querySelectorAll('[data-rotate]').forEach((button) => {
    button.onclick = async () => {
      const answer = prompt(
        `Rotate "${button.dataset.name}".\n\n` +
        'How many hours should the old key keep working? It stops on its own after that, ' +
        'so you can rotate first and redeploy afterwards.\n\n' +
        'Use 0 only if the key is compromised — that breaks callers immediately.', '24');
      if (answer === null) return;
      try {
        const result = await api(`/api-keys/${button.dataset.rotate}/rotate`, {
          method: 'POST',
          body: JSON.stringify({ grace_hours: Number(answer) || 0 }),
        });
        revealKey(result.api_key, result.message);
        await render();
      } catch (err) { alert(err.message); }
    };
  });
}

function revealKey(secret, message) {
  const slot = document.getElementById('key-reveal');
  slot.classList.remove('d-none');
  slot.innerHTML = `
    <div class="fw-medium mb-1">Copy this now — it is not shown again.</div>
    <div class="small text-secondary mb-2">${esc(message || '')}</div>
    <div class="d-flex align-items-center gap-2">
      <code class="mono flex-grow-1 text-break">${esc(secret)}</code>
      <button class="btn btn-sm btn-primary" id="copy-key">Copy</button>
    </div>`;
  document.getElementById('copy-key').onclick = () => {
    navigator.clipboard.writeText(secret);
    document.getElementById('copy-key').textContent = 'Copied';
  };
  renderSnippets(secret);
}

function renderSnippets(secret) {
  const key = secret || 'YOUR_API_KEY';
  const model = aliasCache.find((a) => a.serving)?.alias || 'enterprise-chat';
  const base = baseUrl();

  document.getElementById('snip-python').textContent =
`from openai import OpenAI

client = OpenAI(base_url="${base}", api_key="${key}")

response = client.chat.completions.create(
    model="${model}",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)`;

  document.getElementById('snip-curl').textContent =
`curl ${base}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "Hello"}]
  }'`;

  document.getElementById('snip-js').textContent =
`import OpenAI from "openai";

const client = new OpenAI({ baseURL: "${base}", apiKey: "${key}" });

const response = await client.chat.completions.create({
  model: "${model}",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(response.choices[0].message.content);`;
}

async function renderUsage() {
  try {
    const rows = await api('/usage?hours=24');
    const items = Array.isArray(rows) ? rows : rows.items || [];
    document.getElementById('usage-body').innerHTML = items.length ? `
      <div class="table-responsive"><table class="table table-sm align-middle">
        <thead><tr><th>Model</th><th>Requests</th><th>Prompt</th><th>Completion</th></tr></thead>
        <tbody>${items.slice(0, 20).map((r) => `<tr>
          <td class="mono small">${esc(r.model || r.requested_model || '—')}</td>
          <td class="small">${esc(r.requests ?? r.count ?? '—')}</td>
          <td class="small text-secondary">${esc(r.prompt_tokens ?? '—')}</td>
          <td class="small text-secondary">${esc(r.completion_tokens ?? '—')}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : empty('Nothing yet. Usage appears here after your first call.');
  } catch (err) {
    // A developer without usage.view still needs the rest of the page to work — this is
    // the one section that requires a permission they may not hold.
    document.getElementById('usage-body').innerHTML =
      empty('Your account cannot read usage records. Ask an operator for a report.');
  }
}

/* -------------------------------------------------------------------------- */
/* Session                                                                    */
/* -------------------------------------------------------------------------- */
function showLogin() {
  document.getElementById('login-view').classList.remove('d-none');
  document.getElementById('app-view').classList.add('d-none');
}

async function showApp() {
  const me = await api('/auth/me');
  document.getElementById('login-view').classList.add('d-none');
  document.getElementById('app-view').classList.remove('d-none');
  document.getElementById('who').textContent = `${me.username} · ${me.email}`;
  await render();
}

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

document.getElementById('logout').onclick = () => { token.clear(); showLogin(); };

document.getElementById('copy-base').onclick = () => {
  navigator.clipboard.writeText(baseUrl());
  document.getElementById('copy-base').textContent = 'Copied';
};

document.getElementById('new-key').onclick = async () => {
  // Populated on open so the alias list is current — a model published a minute ago must
  // be scopable now.
  const select = document.getElementById('k-scopes');
  select.innerHTML =
    ['chat', 'embeddings', 'models'].map((s) =>
      `<option value="${s}">${s} — the ${s} endpoints</option>`).join('') +
    aliasCache.map((a) =>
      `<option value="model:${esc(a.alias)}">model:${esc(a.alias)} — only this model</option>`).join('');
  bootstrap.Modal.getOrCreateInstance(document.getElementById('key-modal')).show();
};

document.getElementById('key-form').onsubmit = async (event) => {
  event.preventDefault();
  const error = document.getElementById('key-error');
  error.classList.add('d-none');
  try {
    const clients = await api('/api-clients');
    if (!clients.length) {
      throw new Error(
        'No API application exists to hold this key. Ask an operator to create one.');
    }
    const days = document.getElementById('k-expiry').value;
    const expires = days
      ? new Date(Date.now() + Number(days) * 86400000).toISOString()
      : null;

    const created = await api('/api-keys', {
      method: 'POST',
      body: JSON.stringify({
        client_id: clients[0].id,
        name: document.getElementById('k-name').value.trim(),
        rate_limit_per_minute: Number(document.getElementById('k-rate').value) || 120,
        expires_at: expires,
        scopes: Array.from(document.getElementById('k-scopes').selectedOptions)
          .map((o) => o.value),
      }),
    });
    bootstrap.Modal.getInstance(document.getElementById('key-modal')).hide();
    event.target.reset();
    revealKey(created.api_key, 'Store it in your application configuration now.');
    await render();
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove('d-none');
  }
};

// Offer SSO if the platform has it, so a directory user is not stuck at a password box
// their account does not have.
api('/auth/providers').then((providers) => {
  const redirect = providers.find((p) => p.kind === 'REDIRECT');
  if (!redirect) return;
  document.getElementById('sso-slot').innerHTML =
    `<a class="btn btn-outline-secondary w-100" href="${esc(redirect.start_url)}"
       >${esc(redirect.display_name)}</a>`;
}).catch(() => { /* provider listing unavailable; the password form still works */ });

if (token.get()) {
  showApp().catch(showLogin);
} else {
  showLogin();
}
