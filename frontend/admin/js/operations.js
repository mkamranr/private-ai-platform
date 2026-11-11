/* Private AI Platform — operations: Backup & Restore, Offline Updates, Diagnostics (E7).
 *
 * Loaded after admin.js and uses its helpers (api, esc, pill, empty, guard, alert, can,
 * kpiTile, sectionCard, tableHtml, ago, icon, onModalSubmit, closeModal).
 *
 * Nothing on these pages touches the host directly. Buttons create audited jobs; the
 * host's ops runner (scripts/ops_runner.py) claims them and reports output back. So every
 * page starts with the runner's status: a job requested while no runner is online waits
 * in the queue, and the page says so rather than pretending to make progress.
 */
'use strict';

Object.assign(PILL_TONE, { QUEUED: 'idle', CANCELLED: 'idle' });

const OPS_KIND_LABEL = {
  'backup.create': 'Backup', 'backup.verify': 'Verify backup', 'backup.restore': 'Restore',
  'bundle.verify': 'Verify bundle', upgrade: 'Upgrade', rollback: 'Rollback',
};
const OPS_ACTIVE = new Set(['QUEUED', 'RUNNING']);

const ops = {
  status: null,         // last /ops/runner
  preflight: null,      // { bundle, ok, checks } for the selected bundle
  jobId: null,          // job shown in the log modal
  jobTimer: null,
  confirm: null,        // pending typed confirmation { expected, submit(reason, migrate) }
  diagLoaded: false,
};

const bytes = (n) => {
  if (n === null || n === undefined) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let v = Number(n); let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
};

/** ``20261110T020000Z`` -> a Date, for the stamps backups and bundles are named with. */
function stampDate(stamp) {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$/.exec(stamp || '');
  return m ? new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6])) : null;
}
const stampText = (stamp) => {
  const d = stampDate(stamp);
  return d ? d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : (stamp || '—');
};
const stampAgo = (stamp) => { const d = stampDate(stamp); return d ? ago(d.toISOString()) : ''; };

function duration(job) {
  if (!job.started_at) return '—';
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const s = Math.max(0, Math.round((end - new Date(job.started_at)) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

const jobTarget = (job) => job.params?.backup || job.params?.bundle || job.params?.point || job.result?.backup || '';

/* -------------------------------------------------------------------------- */
/* Runner banner                                                              */
/* -------------------------------------------------------------------------- */
async function loadRunnerStatus() {
  if (!can('diagnostics.view')) return null;
  ops.status = await api('/ops/runner');
  return ops.status;
}

function runnerBanner(status) {
  if (!status) {
    return `<div class="ops-banner tone-idle mb-4">${icon('i-diagnostics')}
      <div><div class="fw-semibold">Runner status hidden</div>
      <div class="small">Viewing the ops runner needs diagnostics.view.</div></div></div>`;
  }
  const active = status.active_job;
  const activeLine = active ? `
    <button class="btn btn-sm btn-light ms-auto" data-ops-job="${esc(active.id)}">
      ${esc(OPS_KIND_LABEL[active.kind] || active.kind)} ${esc(jobTarget(active))} ·
      ${esc(titleCase(active.status))}${active.status === 'RUNNING' ? ` · ${esc(duration(active))}` : ''}
      — view output</button>` : '';
  if (!status.runner) {
    return `<div class="ops-banner tone-warn mb-4">${icon('i-alerts')}
      <div><div class="fw-semibold">No ops runner has connected</div>
      <div class="small">Backups, restores and upgrades run on the host, not in the API container.
        Start <code>make ops-runner</code> on the platform host (or install
        <code>deploy/systemd/ai-platform-ops-runner.service</code>). Requests made now wait in the
        queue until it connects.</div></div>${activeLine}</div>`;
  }
  const r = status.runner;
  if (!status.online) {
    return `<div class="ops-banner tone-crit mb-4">${icon('i-alerts')}
      <div><div class="fw-semibold">Ops runner ${esc(r.name)} is offline</div>
      <div class="small">Last heard from ${esc(ago(r.last_seen_at))} on ${esc(r.host || 'unknown host')}.
        Queued jobs wait until it returns — check <code>journalctl -u ai-platform-ops-runner</code>.
        ${status.queued ? `<b>${status.queued} job(s) queued.</b>` : ''}</div></div>${activeLine}</div>`;
  }
  const free = r.inventory?.disk?.free_bytes;
  return `<div class="ops-banner tone-ok mb-4">${icon('i-check')}
    <div><div class="fw-semibold">Ops runner ${esc(r.name)} is online</div>
    <div class="small">On ${esc(r.host || '?')} · runner ${esc(r.version || '?')} · seen ${esc(ago(r.last_seen_at))}
      ${free !== undefined ? ` · ${esc(bytes(free))} free` : ''}
      ${status.queued ? ` · ${status.queued} queued` : ''}</div></div>${activeLine}</div>`;
}

function jobsTable(jobs, emptyText) {
  if (!jobs.length) return empty(emptyText);
  const rows = jobs.map((j) => `<tr>
      <td>${pill(j.status)}</td>
      <td class="fw-medium">${esc(OPS_KIND_LABEL[j.kind] || j.kind)}</td>
      <td class="font-monospace small">${esc(jobTarget(j)) || '<span class="text-secondary">—</span>'}</td>
      <td class="small">${esc(j.requested_by_name || '—')}
        ${j.reason ? `<div class="text-secondary text-truncate" style="max-width:16rem" title="${esc(j.reason)}">${esc(j.reason)}</div>` : ''}</td>
      <td class="small text-secondary">${esc(ago(j.created_at))}</td>
      <td class="small">${esc(duration(j))}</td>
      <td class="small text-danger text-truncate" style="max-width:18rem" title="${esc(j.error || '')}">${esc(j.error || '')}</td>
      <td class="text-end"><button class="btn btn-sm btn-outline-secondary" data-ops-job="${esc(j.id)}">Output</button></td>
    </tr>`).join('');
  return tableHtml('<th>Status</th><th>Operation</th><th>Target</th><th>Requested by</th><th>When</th><th>Took</th><th>Error</th><th></th>', rows);
}

function wireJobButtons(root) {
  root.querySelectorAll('[data-ops-job]').forEach((b) => { b.onclick = () => openJob(b.dataset.opsJob); });
}

/* -------------------------------------------------------------------------- */
/* Job output modal                                                           */
/* -------------------------------------------------------------------------- */
async function openJob(id) {
  ops.jobId = id;
  const modalEl = document.getElementById('ops-job-modal');
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
  await renderJob();
  clearInterval(ops.jobTimer);
  ops.jobTimer = setInterval(renderJob, 2000);
  modalEl.addEventListener('hidden.bs.modal', () => { clearInterval(ops.jobTimer); ops.jobId = null; }, { once: true });
}

async function renderJob() {
  if (!ops.jobId) return;
  const job = await api(`/ops/jobs/${ops.jobId}`);
  const body = document.getElementById('ops-job-body');
  const log = document.getElementById('ops-job-log');
  const stick = !log || log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  document.getElementById('ops-job-title').innerHTML =
    `${esc(OPS_KIND_LABEL[job.kind] || job.kind)} <span class="font-monospace small text-secondary">${esc(jobTarget(job))}</span> ${pill(job.status)}`;
  const facts = [
    ['Requested by', job.requested_by_name || '—'], ['Reason', job.reason || '—'],
    ['Requested', job.created_at ? new Date(job.created_at).toLocaleString() : '—'],
    ['Runner', job.runner || 'not claimed yet'], ['Duration', duration(job)],
  ];
  body.innerHTML = `
    <dl class="ops-facts mb-3">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    ${job.error ? `<div class="alert alert-danger small py-2">${esc(job.error)}</div>` : ''}
    ${Object.keys(job.result || {}).length ? `<div class="small mb-2"><span class="text-secondary">Result:</span>
      <code>${esc(JSON.stringify(job.result))}</code></div>` : ''}
    ${job.status === 'QUEUED' ? `<div class="alert alert-secondary small py-2">Waiting for the ops runner to claim it.
      ${ops.status && !ops.status.online ? 'The runner is offline.' : ''}</div>` : ''}
    <pre class="ops-log" id="ops-job-log">${esc((job.log || []).join('\n')) || '(no output yet)'}</pre>`;
  const fresh = document.getElementById('ops-job-log');
  if (stick) fresh.scrollTop = fresh.scrollHeight;
  const cancel = document.getElementById('ops-job-cancel');
  const cancellable = job.status === 'QUEUED' || (job.status === 'RUNNING' && ops.status && !ops.status.online);
  cancel.classList.toggle('d-none', !(cancellable && can('backup.manage')));
  cancel.textContent = job.status === 'RUNNING' ? 'Abandon job' : 'Cancel job';
  cancel.onclick = () => guard(cancel, async () => {
    await api(`/ops/jobs/${job.id}/cancel`, { method: 'POST' });
    await renderJob();
    await refresh({ force: true });
  });
  if (!OPS_ACTIVE.has(job.status)) clearInterval(ops.jobTimer);
}

/* -------------------------------------------------------------------------- */
/* Typed confirmation                                                         */
/* -------------------------------------------------------------------------- */
function confirmOps({ title, summary, warning, expected, action, migrate = false, submit }) {
  ops.confirm = { expected, submit };
  document.getElementById('ops-confirm-title').textContent = title;
  document.getElementById('ops-confirm-summary').innerHTML = summary;
  document.getElementById('ops-confirm-warning').innerHTML = warning;
  document.getElementById('ops-confirm-expected').textContent = expected;
  document.getElementById('ops-confirm-migrate-row').classList.toggle('d-none', !migrate);
  document.getElementById('ops-confirm-migrate').checked = true;
  const name = document.getElementById('ops-confirm-name');
  const reason = document.getElementById('ops-confirm-reason');
  const button = document.getElementById('ops-confirm-submit');
  name.value = ''; reason.value = '';
  button.textContent = action;
  button.disabled = true;
  const check = () => { button.disabled = !(name.value.trim() === expected && reason.value.trim().length >= 3); };
  name.oninput = check; reason.oninput = check;
  document.getElementById('ops-confirm-error').classList.add('d-none');
  const modalEl = document.getElementById('ops-confirm-modal');
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
  modalEl.addEventListener('shown.bs.modal', () => reason.focus(), { once: true });
}

onModalSubmit('ops-confirm-form', 'ops-confirm-error', async () => {
  const pending = ops.confirm;
  if (!pending) return;
  const job = await pending.submit(
    document.getElementById('ops-confirm-reason').value.trim(),
    document.getElementById('ops-confirm-name').value.trim(),
    document.getElementById('ops-confirm-migrate').checked,
  );
  closeModal('ops-confirm-modal');
  ops.confirm = null;
  await refresh({ force: true });
  if (job?.id) openJob(job.id);
});

/* -------------------------------------------------------------------------- */
/* Backup & Restore                                                           */
/* -------------------------------------------------------------------------- */
async function renderBackups() {
  const body = document.getElementById('backups-body');
  const createBtn = document.getElementById('ops-backup-create');
  createBtn.classList.toggle('d-none', !can('backup.manage'));
  if (!can('backup.manage')) {
    body.innerHTML = empty('Backups need backup.manage. Ask a super administrator: by default ' +
      'only SUPER_ADMIN holds it, so day-to-day admins cannot take data off the platform.');
    return;
  }
  const [status, list, jobs] = await Promise.all([
    loadRunnerStatus().catch(() => null),
    api('/ops/backups'),
    api('/ops/jobs?limit=100').catch(() => ({ items: [] })),
  ]);
  const backupJobs = jobs.items.filter((j) => j.kind.startsWith('backup.')).slice(0, 15);
  const items = list.items;
  const newest = items.find((b) => b.complete);
  const lastVerified = items.map((b) => b.last_verify).filter((v) => v && v.status === 'SUCCEEDED')
    .sort((a, b) => new Date(b.finished_at) - new Date(a.finished_at))[0];
  const totalSize = items.reduce((n, b) => n + (b.size_bytes || 0), 0);
  const newestAge = newest ? (Date.now() - stampDate(newest.name)) / 3_600_000 : null;
  const canRestore = can('backup.restore');

  const tiles = `<div class="row g-3 mb-4">
    ${kpiTile({ label: 'Backups on host', value: items.length, iconId: 'i-backup', tone: 'blue',
                note: items.length ? `${bytes(totalSize)} in total` : 'none yet' })}
    ${kpiTile({ label: 'Newest backup', value: newest ? stampAgo(newest.name) : '—', iconId: 'i-clock',
                tone: newestAge === null ? 'red' : newestAge > 26 ? 'amber' : 'green',
                note: newest ? stampText(newest.name) : 'take one before any upgrade' })}
    ${kpiTile({ label: 'Last verified', value: lastVerified ? ago(lastVerified.finished_at) : 'never',
                iconId: 'i-shield', tone: lastVerified ? 'green' : 'amber',
                note: lastVerified ? jobTarget(lastVerified) : 'an unverified backup is a hope, not a backup' })}
    ${kpiTile({ label: 'Free disk', value: bytes(status?.runner?.inventory?.disk?.free_bytes),
                iconId: 'i-gpus', tone: 'violet', note: status?.runner ? 'on the platform volume' : 'reported by the runner' })}
  </div>`;

  const rows = items.map((b) => {
    const verify = b.last_verify;
    const verifyPill = !verify ? pill('PENDING', 'Not verified')
      : verify.status === 'SUCCEEDED' ? pill('SUCCEEDED', `Verified ${ago(verify.finished_at)}`)
      : pill(verify.status, verify.status === 'FAILED' ? 'Verify failed' : titleCase(verify.status));
    const key = b.key_matches === true ? pill('HEALTHY', 'Matches')
      : b.key_matches === false ? pill('FAILED', 'Different key') : pill('UNKNOWN', 'Unknown');
    const restoreBlocked = !b.complete || b.key_matches === false;
    return `<tr>
      <td><div class="fw-medium font-monospace">${esc(b.name)}</div>
        <div class="small text-secondary">${esc(stampText(b.name))} · ${esc(stampAgo(b.name))}</div></td>
      <td>${b.complete ? '' : pill('FAILED', 'Incomplete')}
        ${(b.artifacts || []).map((a) => `<span class="chip">${esc(a.replace(/\.(dump|tar)$/, ''))}</span>`).join(' ')}</td>
      <td class="small font-monospace">${esc(b.schema_revision || '—')}</td>
      <td class="small">${esc(b.platform_version || '—')}</td>
      <td class="small text-end">${esc(bytes(b.size_bytes))}</td>
      <td>${key}</td>
      <td>${verifyPill}</td>
      <td class="text-end text-nowrap">
        <button class="btn btn-sm btn-outline-secondary" data-backup-verify="${esc(b.name)}" ${b.complete ? '' : 'disabled'}>Verify</button>
        ${canRestore ? `<button class="btn btn-sm btn-outline-danger" data-backup-restore="${esc(b.name)}"
            ${restoreBlocked ? 'disabled' : ''} title="${restoreBlocked ? esc(b.key_matches === false
              ? 'Taken under a different encryption key; restoring it would leave stored credentials unreadable.'
              : 'Incomplete backup') : 'Replace the platform\'s data with this backup'}">Restore</button>` : ''}
      </td></tr>`;
  }).join('');

  body.innerHTML = runnerBanner(status) + tiles +
    sectionCard('Backups', 'Newest first. The runner lists what is in backups/ on the host.',
      items.length ? tableHtml('<th>Backup</th><th>Contents</th><th>Schema</th><th>Version</th><th class="text-end">Size</th><th>Key</th><th>Verified</th><th></th>', rows)
        : `<div class="card-body pt-0">${empty(list.runner_online ? 'No backups on the host yet. Take one now — and always before an upgrade.'
          : 'No backups reported. The runner reports what is on disk once it connects.')}</div>`) +
    sectionCard('Recent operations', 'Backups, verifications and restores, with who asked and why.',
      `<div class="${backupJobs.length ? '' : 'card-body pt-0'}">${jobsTable(backupJobs, 'Nothing has been requested yet.')}</div>`) +
    `<p class="small text-secondary">${icon('i-info')} A restore replaces the database and object storage with
      the backup's, stops the API while it runs, and is all-or-nothing. Vector collections are
      restored separately, see docs/backup.md.</p>`;

  wireJobButtons(body);
  createBtn.onclick = () => guard(createBtn, async () => {
    const job = await api('/ops/backups', { method: 'POST', body: JSON.stringify({ reason: 'Requested from the admin console' }) });
    await refresh({ force: true });
    openJob(job.id);
  });
  body.querySelectorAll('[data-backup-verify]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      const job = await api(`/ops/backups/${encodeURIComponent(b.dataset.backupVerify)}/verify`,
        { method: 'POST', body: JSON.stringify({}) });
      await refresh({ force: true });
      openJob(job.id);
    });
  });
  body.querySelectorAll('[data-backup-restore]').forEach((b) => {
    b.onclick = () => {
      const backup = items.find((x) => x.name === b.dataset.backupRestore);
      const verified = backup.last_verify?.status === 'SUCCEEDED';
      confirmOps({
        title: 'Restore backup',
        expected: backup.name,
        action: 'Restore',
        summary: `<dl class="ops-facts mb-0">
          <dt>Backup</dt><dd class="font-monospace">${esc(backup.name)}</dd>
          <dt>Taken</dt><dd>${esc(stampText(backup.name))} (${esc(stampAgo(backup.name))})</dd>
          <dt>Schema</dt><dd class="font-monospace">${esc(backup.schema_revision || '—')}</dd>
          <dt>Verified</dt><dd>${verified ? 'Yes' : '<span class="text-danger">No, verify it first</span>'}</dd></dl>`,
        warning: `${icon('i-alerts')} <b>This replaces all platform data</b> with the backup's. Everything
          changed since ${esc(stampText(backup.name))} is lost, including users, keys and audit records.
          The API stops while it runs. backup.py verifies the backup again before touching anything.`,
        submit: (reason, confirm) => api(`/ops/backups/${encodeURIComponent(backup.name)}/restore`,
          { method: 'POST', body: JSON.stringify({ reason, confirm }) }),
      });
    };
  });
}

/* -------------------------------------------------------------------------- */
/* Offline Updates                                                            */
/* -------------------------------------------------------------------------- */
const SIGNATURE_PILL = {
  valid: ['SUCCEEDED', 'Signed · trusted'], untrusted: ['WARNING', 'Signed · no trust key'],
  unsigned: ['WARNING', 'Unsigned'], invalid: ['FAILED', 'Bad signature'], error: ['FAILED', 'Unreadable'],
};
const CHECK_ICON = { pass: 'i-check', warn: 'i-alerts', fail: 'i-x' };

async function renderUpdates() {
  const body = document.getElementById('updates-body');
  if (!can('system.upgrade')) {
    body.innerHTML = empty('Offline updates need system.upgrade, which only SUPER_ADMIN holds by default.');
    return;
  }
  const [status, list, rollbacks, jobs] = await Promise.all([
    loadRunnerStatus().catch(() => null),
    api('/ops/bundles'),
    api('/ops/rollbacks'),
    api('/ops/jobs?limit=100').catch(() => ({ items: [] })),
  ]);
  const updateJobs = jobs.items.filter((j) => ['bundle.verify', 'upgrade', 'rollback'].includes(j.kind)).slice(0, 15);
  if (ops.preflight && list.items.some((b) => b.name === ops.preflight.bundle)) {
    ops.preflight = await api(`/ops/bundles/${encodeURIComponent(ops.preflight.bundle)}/preflight`).catch(() => null);
  } else {
    ops.preflight = null;
  }
  const installed = list.installed;
  const trust = list.trust_key;
  const importDir = status?.runner?.inventory?.import_dir || '<platform>/updates';

  const installedCard = `<div class="row g-3 mb-4">
    <div class="col-lg-6"><div class="card h-100"><div class="card-body">
      <div class="ops-card-head">${icon('i-updates')}<h6 class="mb-0">Installed</h6></div>
      ${installed ? `<dl class="ops-facts mb-0">
        <dt>Version</dt><dd class="fw-semibold">${esc(installed.platform_version || '—')}</dd>
        <dt>Bundle</dt><dd class="font-monospace">${esc(installed.created_at || '—')}</dd>
        <dt>Signed by</dt><dd class="font-monospace small">${esc(installed.signed_by || 'unsigned bundle')}</dd></dl>`
        : `<p class="small text-secondary mb-0">${list.runner_online ? 'This host was not installed from an offline bundle (no .bundle-manifest.json), so it is a source checkout. Upgrades apply to bundle installs.' : 'Reported by the ops runner once it connects.'}</p>`}
    </div></div></div>
    <div class="col-lg-6"><div class="card h-100"><div class="card-body">
      <div class="ops-card-head">${icon('i-shield')}<h6 class="mb-0">Bundle trust</h6></div>
      ${trust?.configured ? `<p class="small mb-1">${pill('SUCCEEDED', 'Signatures required')} Bundles must be signed by:</p>
        <code class="small d-block text-break">${esc(trust.fingerprint || '?')}</code>`
        : `<p class="small mb-1">${pill('WARNING', 'No trust key')} Bundles are accepted without a signature check.</p>
        <p class="small text-secondary mb-0">Install the build site's public key to require signed bundles:
        <code>offline/upgrade.sh &lt;bundle&gt; &lt;root&gt; --trust-key bundle-signing.pub</code>. Get the key
        through a channel other than the bundle itself.</p>`}
    </div></div></div></div>`;

  const rows = list.items.map((b) => {
    const [tone, label] = SIGNATURE_PILL[b.signature] || ['UNKNOWN', b.signature];
    const v = b.last_verify;
    const verifyPill = !v ? pill('PENDING', 'Not verified')
      : v.status === 'SUCCEEDED' ? pill('SUCCEEDED', `Verified ${ago(v.finished_at)}`) : pill(v.status, titleCase(v.status));
    const selected = ops.preflight?.bundle === b.name;
    return `<tr class="${selected ? 'table-active' : ''}">
      <td><div class="fw-medium font-monospace">${esc(b.name)}</div>
        <div class="small text-secondary">built ${esc(stampText(b.created_at))}</div></td>
      <td class="fw-semibold">${esc(b.platform_version || '—')}</td>
      <td class="small text-end">${esc(bytes(b.size_bytes))}</td>
      <td>${pill(tone, label)}${b.signer_fingerprint ? `<div class="small text-secondary font-monospace text-truncate" style="max-width:12rem" title="${esc(b.signer_fingerprint)}">${esc(b.signer_fingerprint)}</div>` : ''}</td>
      <td>${b.has_sbom ? pill('SUCCEEDED', 'SBOM') : pill('UNKNOWN', 'None')}</td>
      <td>${verifyPill}</td>
      <td class="text-end text-nowrap">
        <button class="btn btn-sm btn-outline-secondary" data-bundle-verify="${esc(b.name)}">Verify</button>
        <button class="btn btn-sm ${selected ? 'btn-primary' : 'btn-outline-primary'}" data-bundle-preflight="${esc(b.name)}">Preflight</button>
      </td></tr>`;
  }).join('');

  const pf = ops.preflight;
  const preflightCard = pf ? sectionCard(`Preflight · ${pf.bundle}`,
    pf.ok ? 'Every blocking check passes. Warnings are worth reading before you continue.'
      : 'Fix the failing checks before upgrading. The API refuses an upgrade while any check fails.',
    `<div class="card-body pt-0"><ul class="check-list">${pf.checks.map((c) => `
      <li class="check-${esc(c.status)}">${icon(CHECK_ICON[c.status])}
        <div><div class="fw-medium">${esc(c.label)}</div><div class="small text-secondary">${esc(c.detail)}</div></div></li>`).join('')}
    </ul>
    <div class="d-flex gap-2 justify-content-end">
      <button class="btn btn-danger btn-sm" id="ops-upgrade" ${pf.ok ? '' : 'disabled'}>Upgrade to ${esc(pf.bundle)}</button>
    </div></div>`) : '';

  const pointRows = rollbacks.items.map((p) => `<tr>
      <td class="font-monospace fw-medium">${esc(p.name)}</td>
      <td class="small">${esc(p.platform_version || '—')}</td>
      <td class="small text-secondary">${esc(stampText(p.name))}</td>
      <td class="text-end"><button class="btn btn-sm btn-outline-danger" data-rollback="${esc(p.name)}">Roll back</button></td>
    </tr>`).join('');

  body.innerHTML = runnerBanner(status) + installedCard +
    `<p class="small text-secondary mb-2">Import directory: <code class="text-break">${esc(importDir)}</code></p>` +
    sectionCard('Bundles waiting on the host',
      'Copy a bundle directory from removable media into the import directory on the host and it appears here.',
      list.items.length ? tableHtml('<th>Bundle</th><th>Version</th><th class="text-end">Size</th><th>Signature</th><th>SBOM</th><th>Archives</th><th></th>', rows)
        : `<div class="card-body pt-0">${empty(list.runner_online ? `No bundles in ${importDir}. Build one with make bundle on the build machine, carry it over, and copy the directory there.`
          : 'Bundles are listed by the ops runner once it connects.')}</div>`) +
    preflightCard +
    sectionCard('Rollback points', 'Each upgrade snapshots the tree and image names it replaces. Rolling back restores code, not data: restore the pre-upgrade backup as well if migrations ran.',
      rollbacks.items.length ? tableHtml('<th>Point</th><th>Version</th><th>Taken</th><th></th>', pointRows)
        : `<div class="card-body pt-0">${empty('No rollback points. The first upgrade creates one.')}</div>`) +
    sectionCard('Recent operations', '',
      `<div class="${updateJobs.length ? '' : 'card-body pt-0'}">${jobsTable(updateJobs, 'No verifications, upgrades or rollbacks yet.')}</div>`);

  wireJobButtons(body);
  body.querySelectorAll('[data-bundle-verify]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      const job = await api(`/ops/bundles/${encodeURIComponent(b.dataset.bundleVerify)}/verify`, { method: 'POST', body: JSON.stringify({}) });
      await refresh({ force: true });
      openJob(job.id);
    });
  });
  body.querySelectorAll('[data-bundle-preflight]').forEach((b) => {
    b.onclick = () => guard(b, async () => {
      ops.preflight = await api(`/ops/bundles/${encodeURIComponent(b.dataset.bundlePreflight)}/preflight`);
      await renderUpdates();
    });
  });
  const upgrade = document.getElementById('ops-upgrade');
  if (upgrade) {
    upgrade.onclick = () => {
      const bundle = list.items.find((b) => b.name === pf.bundle);
      confirmOps({
        title: 'Upgrade the platform',
        expected: bundle.name,
        action: 'Upgrade',
        migrate: true,
        summary: `<dl class="ops-facts mb-0">
          <dt>From</dt><dd>${esc(installed?.platform_version || '?')} <span class="font-monospace small text-secondary">${esc(installed?.created_at || '')}</span></dd>
          <dt>To</dt><dd class="fw-semibold">${esc(bundle.platform_version)} <span class="font-monospace small text-secondary">${esc(bundle.name)}</span></dd>
          <dt>Signature</dt><dd>${esc((SIGNATURE_PILL[bundle.signature] || [0, bundle.signature])[1])}</dd></dl>`,
        warning: `${icon('i-alerts')} The platform <b>stops</b> while images load and the tree is replaced,
          typically for several minutes. upgrade.sh takes a backup and a rollback point first, and refuses
          to continue if the backup fails.`,
        submit: (reason, confirm, migrate) => api('/ops/upgrades',
          { method: 'POST', body: JSON.stringify({ bundle: bundle.name, confirm, reason, migrate }) }),
      });
    };
  }
  body.querySelectorAll('[data-rollback]').forEach((b) => {
    b.onclick = () => confirmOps({
      title: 'Roll back',
      expected: b.dataset.rollback,
      action: 'Roll back',
      summary: `<p class="mb-0">Restore the tree and image names snapshotted at <code>${esc(b.dataset.rollback)}</code>.</p>`,
      warning: `${icon('i-alerts')} Code only: the database keeps any migrations the newer version applied.
        If it did, restore the backup taken before the upgrade too.`,
      submit: (reason, confirm) => api('/ops/rollbacks',
        { method: 'POST', body: JSON.stringify({ point: b.dataset.rollback, confirm, reason }) }),
    });
  });
}

/* -------------------------------------------------------------------------- */
/* Diagnostics                                                                */
/* -------------------------------------------------------------------------- */
const DEP_PILL = { ok: ['HEALTHY', 'OK'], unavailable: ['FAILED', 'Unavailable'], timeout: ['FAILED', 'Timeout'], not_configured: ['UNKNOWN', 'Not configured'] };

async function renderDiagnostics({ force = false } = {}) {
  const body = document.getElementById('diagnostics-body');
  if (!can('diagnostics.view')) {
    body.innerHTML = empty('Diagnostics need diagnostics.view (INFRA_ADMIN, AUDITOR and SUPER_ADMIN hold it).');
    return;
  }
  // Health refreshes on every poll; versions and configuration only when asked, so the
  // page does not re-query every component every five seconds.
  if (!force && ops.diagLoaded) {
    const health = await api('/diagnostics/health');
    const slot = document.getElementById('diag-health');
    if (slot) slot.innerHTML = healthGrid(health);
    return;
  }
  const [health, inventory, config, status] = await Promise.all([
    api('/diagnostics/health'), api('/diagnostics/inventory'), api('/diagnostics/config'),
    loadRunnerStatus().catch(() => null),
  ]);
  ops.diagLoaded = true;

  const components = inventory.components.map((c) => `<tr>
      <td class="fw-medium">${esc(c.component)}</td>
      <td class="font-monospace small">${esc(c.version || '—')}</td>
      <td class="small text-secondary">${esc(c.detail || '')}</td>
      <td>${c.status === 'ok' ? pill('HEALTHY', 'OK') : pill(c.status === 'unknown' ? 'UNKNOWN' : 'FAILED', titleCase(c.status))}</td>
    </tr>`).join('');
  const nodes = inventory.nodes.map((n) => `<tr>
      <td class="fw-medium">${esc(n.name)}</td><td>${pill(n.status)}</td>
      <td class="font-monospace small">${esc(n.agent_version || '—')}</td>
      <td class="small">${esc(n.docker_version || '—')}</td>
      <td class="small">${esc(n.nvidia_driver_version ? `${n.nvidia_driver_version} / CUDA ${n.cuda_version || '?'}` : '—')}</td>
      <td class="small text-secondary">${esc(ago(n.last_seen_at))}</td></tr>`).join('');
  const schema = inventory.schema_current === true ? pill('HEALTHY', 'At head')
    : inventory.schema_current === false ? pill('WARNING', 'Behind: run alembic upgrade head') : pill('UNKNOWN', 'Unknown');

  body.innerHTML = `
    <div class="row g-3 mb-4">
      ${kpiTile({ label: 'Platform', value: inventory.platform_version, iconId: 'i-diagnostics', tone: 'blue', note: inventory.environment })}
      ${kpiTile({ label: 'Readiness', value: health.ready ? 'Ready' : 'Not ready', iconId: 'i-pulse',
                  tone: health.ready ? 'green' : 'red', valueClass: health.ready ? '' : 'text-danger',
                  note: health.failing_required.length ? `failing: ${health.failing_required.join(', ')}` : 'all required dependencies up' })}
      ${kpiTile({ label: 'Schema', value: (inventory.schema_revision || '—').slice(0, 12), iconId: 'i-backup', tone: 'violet',
                  note: inventory.schema_current === false ? `head is ${inventory.schema_head}` : 'migrations applied' })}
      ${kpiTile({ label: 'Ops runner', value: status?.online ? 'Online' : status?.runner ? 'Offline' : 'Not connected',
                  iconId: 'i-nodes', tone: status?.online ? 'green' : 'amber', note: status?.runner?.name || 'scripts/ops_runner.py' })}
    </div>
    ${sectionCard('Dependencies', 'Probed concurrently, each bounded by a timeout. Refreshes every few seconds.',
      `<div class="card-body pt-0" id="diag-health">${healthGrid(health)}</div>`)}
    <div class="row g-4">
      <div class="col-xl-7">${sectionCard('Version inventory', `Collected ${new Date(inventory.collected_at).toLocaleTimeString()}. Schema ${inventory.schema_revision || '?'} ${''}`,
        `<div class="px-3 pb-2">${schema}${inventory.installed_bundle ? ` <span class="small text-secondary ms-2">bundle ${esc(inventory.installed_bundle.created_at)}</span>` : ''}</div>` +
        tableHtml('<th>Component</th><th>Version</th><th>Detail</th><th></th>', components))}</div>
      <div class="col-xl-5">${sectionCard('Support bundle',
        'A zip for the vendor or your own support team: health, versions, redacted configuration, recent alerts, operations history and recent logs.',
        `<div class="card-body pt-0">
          <ul class="small text-secondary ps-3 mb-3">
            <li>Secrets are replaced with <code>[REDACTED]</code>: passwords, tokens, keys, credentials in URLs, bearer tokens, JWTs and private keys.</li>
            <li>${config.redacted_fields} configuration values are redacted, as shown below.</li>
            <li>Logs come from Loki when the monitoring profile is running.</li>
            <li>Each export is recorded in the audit log.</li>
          </ul>
          <div class="form-check mb-3"><input class="form-check-input" type="checkbox" id="diag-logs" checked>
            <label class="form-check-label small" for="diag-logs">Include the last hours of platform logs</label></div>
          <button class="btn btn-primary btn-sm" id="diag-download">${icon('i-updates')} Download support bundle</button>
        </div>`)}</div>
    </div>
    ${inventory.nodes.length ? sectionCard('Nodes', 'Agent, Docker and driver versions as last reported.',
      tableHtml('<th>Node</th><th>Status</th><th>Agent</th><th>Docker</th><th>Driver</th><th>Seen</th>', nodes)) : ''}
    ${sectionCard('Effective configuration', `What this backend is running with, after environment overrides. ${config.redacted_fields} secret values redacted.`,
      `<div class="card-body pt-0"><input class="form-control form-control-sm mb-2" id="diag-config-filter"
         placeholder="Filter, e.g. gateway or ops" aria-label="Filter configuration">
       <pre class="ops-log ops-config" id="diag-config">${esc(JSON.stringify(config.config, null, 2))}</pre></div>`)}`;

  const filter = document.getElementById('diag-config-filter');
  filter.oninput = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = q ? Object.fromEntries(Object.entries(config.config).filter(([k, v]) =>
      k.toLowerCase().includes(q) || JSON.stringify(v).toLowerCase().includes(q))) : config.config;
    document.getElementById('diag-config').textContent = JSON.stringify(shown, null, 2);
  };
  const download = document.getElementById('diag-download');
  download.onclick = () => guard(download, async () => {
    const logs = document.getElementById('diag-logs').checked;
    const response = await fetch(`${API}/diagnostics/bundle?logs=${logs}`, {
      method: 'POST', headers: { Authorization: `Bearer ${token.get()}` },
    });
    if (!response.ok) throw new Error(`Support bundle failed (${response.status})`);
    const blob = await response.blob();
    const name = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || 'support-bundle.zip';
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement('a'), { href: url, download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    alert(`Downloaded ${name} (${bytes(blob.size)}). The export is recorded in the audit log.`, 'success');
  });
}

function healthGrid(health) {
  return `<div class="row g-3">${health.dependencies.map((d) => {
    const [tone, label] = DEP_PILL[d.state] || ['UNKNOWN', d.state];
    return `<div class="col-sm-6 col-xl-3"><div class="dep-tile">
      <div class="d-flex justify-content-between align-items-center">
        <span class="fw-semibold text-capitalize">${esc(d.name)}</span>${pill(tone, label)}</div>
      <div class="small text-secondary mt-1">${d.latency_ms !== null ? `${esc(d.latency_ms)} ms` : '—'}
        · ${d.required ? 'required' : 'optional'}${d.detail ? ` · ${esc(d.detail)}` : ''}</div>
    </div></div>`;
  }).join('')}</div>`;
}

document.getElementById('diag-refresh').onclick = () => { ops.diagLoaded = false; refresh({ force: true }); };

RENDERERS.backups = renderBackups;
RENDERERS.updates = renderUpdates;
RENDERERS.diagnostics = renderDiagnostics;
