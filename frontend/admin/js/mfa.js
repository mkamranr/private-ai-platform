/**
 * Two-factor authentication in the console (expansion E15).
 *
 * Three places:
 *  - the second sign-in step: a code, or a recovery code;
 *  - the forced set-up, when the platform's policy requires MFA for an account that has
 *    none yet: QR code, first code, recovery codes, then into the console;
 *  - the account dialog behind the user badge: set up, turn off, replace recovery codes.
 *
 * The QR code is an SVG rendered by the server (no browser library, no network). It is
 * the server's own output, not user input, which is why it is inserted as markup.
 *
 * E20 adds passkeys (WebAuthn) as another answer to the second step, and "trust this
 * browser": a random token the server hands back, kept in localStorage per username and
 * sent with the password next time. Only its hash is stored on the server; signing out
 * everywhere or changing the password makes it worthless.
 *
 * Relies on admin.js globals: api, token, showApp, esc, alert, guard, currentUser.
 */

let mfaChallenge = null;  // { mfa_token, enrolment_required, username, ... } while open

/* ---------------------------------------------------------------- E20 helpers */

const DEVICE_KEY_PREFIX = 'aip.trusted-device.';

function trustedDeviceToken(username) {
  try { return localStorage.getItem(DEVICE_KEY_PREFIX + (username || '').toLowerCase()); }
  catch { return null; }
}

function keepDeviceToken(username, result) {
  if (!result.device_token || !username) return;
  try { localStorage.setItem(DEVICE_KEY_PREFIX + username.toLowerCase(), result.device_token); }
  catch { /* private window: the browser is simply not remembered */ }
}

function forgetDeviceToken(username) {
  try { localStorage.removeItem(DEVICE_KEY_PREFIX + (username || '').toLowerCase()); }
  catch { /* nothing stored */ }
}

function b64urlToBuffer(value) {
  const base64 = value.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4);
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0)).buffer;
}

function bufferToB64url(buffer) {
  let text = '';
  new Uint8Array(buffer).forEach((b) => { text += String.fromCharCode(b); });
  return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function passkeysInBrowser() {
  return !!(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);
}

/** The server's JSON options -> what navigator.credentials wants (ArrayBuffers). */
function decodeCreationOptions(options) {
  if (PublicKeyCredential.parseCreationOptionsFromJSON) {
    return PublicKeyCredential.parseCreationOptionsFromJSON(options);
  }
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    user: { ...options.user, id: b64urlToBuffer(options.user.id) },
    excludeCredentials: (options.excludeCredentials || [])
      .map((c) => ({ ...c, id: b64urlToBuffer(c.id) })),
  };
}

function decodeRequestOptions(options) {
  if (PublicKeyCredential.parseRequestOptionsFromJSON) {
    return PublicKeyCredential.parseRequestOptionsFromJSON(options);
  }
  return {
    ...options,
    challenge: b64urlToBuffer(options.challenge),
    allowCredentials: (options.allowCredentials || [])
      .map((c) => ({ ...c, id: b64urlToBuffer(c.id) })),
  };
}

/** A PublicKeyCredential -> JSON the server can verify. */
function credentialJSON(credential) {
  if (typeof credential.toJSON === 'function') return credential.toJSON();
  const r = credential.response;
  const response = { clientDataJSON: bufferToB64url(r.clientDataJSON) };
  if (r.attestationObject) {
    response.attestationObject = bufferToB64url(r.attestationObject);
    response.transports = typeof r.getTransports === 'function' ? r.getTransports() : [];
  } else {
    response.authenticatorData = bufferToB64url(r.authenticatorData);
    response.signature = bufferToB64url(r.signature);
    response.userHandle = r.userHandle ? bufferToB64url(r.userHandle) : null;
  }
  return {
    id: credential.id,
    rawId: bufferToB64url(credential.rawId),
    type: credential.type,
    response,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

/** The browser's own refusals ("cancelled", "timed out") in plain words. */
function passkeyBrowserError(err) {
  if (err && err.name === 'NotAllowedError') return 'The passkey request was cancelled or timed out.';
  if (err && err.name === 'InvalidStateError') return 'That passkey is already on your account.';
  return (err && err.message) || 'The passkey did not work.';
}

/* ---------------------------------------------------------------- sign-in step */

function startMfaStep(challenge) {
  mfaChallenge = challenge;
  document.getElementById('password-step').classList.add('d-none');
  document.getElementById('mfa-step').classList.remove('d-none');
  document.getElementById('mfa-codes').classList.add('d-none');
  document.getElementById('mfa-form').classList.remove('d-none');
  document.getElementById('mfa-error').classList.add('d-none');
  const code = document.getElementById('mfa-code');
  code.value = '';

  const enrol = document.getElementById('mfa-enrol');
  if (challenge.enrolment_required) {
    document.getElementById('mfa-title').textContent = 'Set up two-factor authentication';
    document.getElementById('mfa-lead').textContent =
      'This platform requires an authenticator app for your account. Scan the code with ' +
      'one (Microsoft Authenticator, Google Authenticator, FreeOTP, a password manager), ' +
      'then enter the six digits it shows.';
    document.getElementById('mfa-hint').classList.add('d-none');
    document.getElementById('mfa-submit').textContent = 'Turn on and sign in';
    enrol.classList.remove('d-none');
    enrol.innerHTML = '<div class="text-secondary small text-center">Preparing…</div>';
    api('/auth/mfa/enrol/start', {
      method: 'POST',
      body: JSON.stringify({ mfa_token: challenge.mfa_token }),
    }).then((setup) => { enrol.innerHTML = mfaSetupBlock(setup); })
      .catch((err) => showMfaError(err.message));
  } else {
    document.getElementById('mfa-title').textContent = 'Two-factor authentication';
    document.getElementById('mfa-lead').textContent =
      'Enter the six-digit code from your authenticator app.';
    document.getElementById('mfa-hint').classList.remove('d-none');
    document.getElementById('mfa-submit').textContent = 'Verify';
    enrol.classList.add('d-none');
    enrol.innerHTML = '';
  }
  // E20: passkey and "trust this browser", only when the server offers them.
  const passkey = document.getElementById('mfa-passkey');
  passkey.classList.toggle(
    'd-none', challenge.enrolment_required || !challenge.passkey_available || !passkeysInBrowser(),
  );
  const days = challenge.enrolment_required ? 0 : (challenge.remember_days || 0);
  document.getElementById('mfa-remember-row').classList.toggle('d-none', days <= 0);
  document.getElementById('mfa-remember').checked = false;
  document.getElementById('mfa-remember-label').textContent =
    `Trust this browser for ${days} day${days === 1 ? '' : 's'}`;
  code.focus();
}

function rememberChosen() {
  return !document.getElementById('mfa-remember-row').classList.contains('d-none') &&
    document.getElementById('mfa-remember').checked;
}

document.getElementById('mfa-passkey').onclick = async (event) => {
  if (!mfaChallenge) return;
  const button = event.target;
  document.getElementById('mfa-error').classList.add('d-none');
  button.disabled = true;
  try {
    const options = await api('/auth/mfa/passkey/options', {
      method: 'POST',
      body: JSON.stringify({ mfa_token: mfaChallenge.mfa_token }),
    });
    let credential;
    try {
      credential = await navigator.credentials.get({
        publicKey: decodeRequestOptions(options.public_key),
      });
    } catch (err) { showMfaError(passkeyBrowserError(err)); return; }
    const result = await api('/auth/mfa/passkey/verify', {
      method: 'POST',
      body: JSON.stringify({
        mfa_token: mfaChallenge.mfa_token,
        challenge_token: options.challenge_token,
        credential: credentialJSON(credential),
        remember_device: rememberChosen(),
      }),
    });
    keepDeviceToken(mfaChallenge.username, result);
    token.set(result.access_token);
    resetMfaStep();
    await showApp();
  } catch (err) {
    showMfaError(err.message);
  } finally {
    button.disabled = false;
  }
};

function resetMfaStep() {
  mfaChallenge = null;
  document.getElementById('mfa-step').classList.add('d-none');
  document.getElementById('password-step').classList.remove('d-none');
  document.getElementById('mfa-enrol').innerHTML = '';
  document.getElementById('mfa-codes').innerHTML = '';
}

function showMfaError(message) {
  const box = document.getElementById('mfa-error');
  box.textContent = message;
  box.classList.remove('d-none');
}

/** QR code, the key for typing in by hand, and nothing else. */
function mfaSetupBlock(setup) {
  const grouped = setup.secret.replace(/(.{4})/g, '$1 ').trim();
  return `
    <div class="mfa-qr mx-auto mb-2" role="img" aria-label="QR code for your authenticator app"
         >${setup.qr_svg}</div>
    <div class="small text-secondary text-center">
      Can't scan it? Enter this key instead:<br>
      <code class="user-select-all fs-6">${esc(grouped)}</code>
    </div>`;
}

/** Recovery codes, shown once, with a copy button and a deliberate "done". */
function recoveryCodesBlock(codes, doneLabel) {
  return `
    <div class="alert alert-warning small mb-2">
      <strong>Save these recovery codes now.</strong> Each one signs you in once if you lose
      your phone. They are not shown again.
    </div>
    <ol class="mfa-recovery font-monospace small mb-2">
      ${codes.map((c) => `<li>${esc(c)}</li>`).join('')}
    </ol>
    <div class="d-flex gap-2">
      <button type="button" class="btn btn-sm btn-outline-secondary" data-mfa-copy>Copy</button>
      <button type="button" class="btn btn-sm btn-primary ms-auto" data-mfa-done
              >${esc(doneLabel)}</button>
    </div>`;
}

function wireRecoveryCodes(container, codes, onDone) {
  container.querySelector('[data-mfa-copy]').onclick = async (event) => {
    try {
      await navigator.clipboard.writeText(codes.join('\n'));
      event.target.textContent = 'Copied';
    } catch {
      event.target.textContent = 'Select and copy them';
    }
  };
  container.querySelector('[data-mfa-done]').onclick = onDone;
}

document.getElementById('mfa-form').onsubmit = async (event) => {
  event.preventDefault();
  if (!mfaChallenge) return;
  const button = document.getElementById('mfa-submit');
  const code = document.getElementById('mfa-code').value.trim();
  document.getElementById('mfa-error').classList.add('d-none');
  if (!code) { showMfaError('Enter the code.'); return; }
  button.disabled = true;
  try {
    if (mfaChallenge.enrolment_required) {
      const result = await api('/auth/mfa/enrol/finish', {
        method: 'POST',
        body: JSON.stringify({ mfa_token: mfaChallenge.mfa_token, code }),
      });
      token.set(result.access_token);
      document.getElementById('mfa-form').classList.add('d-none');
      document.getElementById('mfa-enrol').classList.add('d-none');
      document.getElementById('mfa-lead').textContent = 'Two-factor authentication is on.';
      const box = document.getElementById('mfa-codes');
      box.innerHTML = recoveryCodesBlock(result.recovery_codes, 'I have saved them — continue');
      box.classList.remove('d-none');
      wireRecoveryCodes(box, result.recovery_codes, async () => {
        resetMfaStep();
        await showApp();
      });
      return;
    }
    const result = await api('/auth/mfa/verify', {
      method: 'POST',
      body: JSON.stringify({
        mfa_token: mfaChallenge.mfa_token, code, remember_device: rememberChosen(),
      }),
    });
    keepDeviceToken(mfaChallenge.username, result);
    token.set(result.access_token);
    resetMfaStep();
    await showApp();
  } catch (err) {
    showMfaError(err.message);
    if (/sign in again/i.test(err.message)) {
      document.getElementById('mfa-submit').disabled = true;
    }
  } finally {
    if (!/sign in again/i.test(document.getElementById('mfa-error').textContent)) {
      button.disabled = false;
    }
  }
};

document.getElementById('mfa-restart').onclick = () => {
  resetMfaStep();
  document.getElementById('mfa-submit').disabled = false;
  document.getElementById('password').focus();
};

/* ---------------------------------------------------------------- account dialog */

document.getElementById('user-badge').onclick = () => openMfaDialog();

async function openMfaDialog() {
  const element = document.getElementById('mfa-modal');
  bootstrap.Modal.getOrCreateInstance(element).show();
  await renderMfaDialog();
}

async function renderMfaDialog() {
  const body = document.getElementById('mfa-modal-body');
  body.innerHTML = '<div class="text-secondary small">Loading…</div>';
  let status;
  try {
    status = await api('/auth/mfa');
  } catch (err) {
    body.innerHTML = `<div class="alert alert-danger small">${esc(err.message)}</div>`;
    return;
  }

  if (!status.available) {
    body.innerHTML = `<p class="small mb-0">You sign in through your organisation's identity
      provider, so two-factor authentication is set up there, not here.</p>`;
    return;
  }

  if (!status.enabled) {
    body.innerHTML = `
      <p class="small">Two-factor authentication is <strong>off</strong>. With it on, signing
        in needs a six-digit code from an authenticator app on your phone as well as your
        password.</p>
      ${status.required ? `<div class="alert alert-warning small">The platform requires it
        for your account. You will be asked to set it up at your next sign-in.</div>` : ''}
      <form id="mfa-setup-form" class="mt-3">
        <label class="form-label small text-secondary" for="mfa-setup-password"
               >Confirm your password to start</label>
        <input class="form-control" type="password" id="mfa-setup-password"
               autocomplete="current-password" required>
        <div class="alert alert-danger small py-2 mt-2 d-none" id="mfa-dialog-error"></div>
        <button class="btn btn-primary mt-3" type="submit">Set up</button>
      </form>`;
    document.getElementById('mfa-setup-form').onsubmit = (event) => {
      event.preventDefault();
      const button = event.submitter;
      guard(button, async () => {
        try {
          const setup = await api('/auth/mfa/setup', {
            method: 'POST',
            body: JSON.stringify({ password: document.getElementById('mfa-setup-password').value }),
          });
          renderMfaConfirm(setup);
        } catch (err) { dialogError(err.message); }
      });
    };
    return;
  }

  const since = status.enabled_at ? new Date(status.enabled_at).toLocaleDateString() : '';
  const low = status.recovery_codes_left <= 3;
  body.innerHTML = `
    <p class="small">Two-factor authentication is <strong class="text-success">on</strong>
      ${since ? `since ${esc(since)}` : ''}.</p>
    <p class="small ${low ? 'text-warning' : 'text-secondary'}">
      ${status.recovery_codes_left} recovery code(s) left.
      ${low ? 'Replace them before you run out.' : ''}</p>
    <form id="mfa-manage-form">
      <label class="form-label small text-secondary" for="mfa-manage-code"
             >Current code from your app</label>
      <input class="form-control font-monospace" id="mfa-manage-code" inputmode="numeric"
             autocomplete="one-time-code" maxlength="32" required>
      <div class="alert alert-danger small py-2 mt-2 d-none" id="mfa-dialog-error"></div>
      <div class="d-flex flex-wrap gap-2 mt-3">
        <button class="btn btn-outline-secondary btn-sm" type="button" id="mfa-regen"
                >Replace recovery codes</button>
        ${status.required
          ? '<span class="small text-secondary align-self-center ms-auto">Required by the ' +
            'platform, so it cannot be turned off.</span>'
          : '<button class="btn btn-outline-danger btn-sm ms-auto" type="button" ' +
            'id="mfa-off">Turn off</button>'}
      </div>
    </form>
    <div id="mfa-factors"></div>`;
  document.getElementById('mfa-manage-form').onsubmit = (event) => event.preventDefault();
  renderSecondFactors(document.getElementById('mfa-factors'), status);

  document.getElementById('mfa-regen').onclick = (event) => guard(event.target, async () => {
    try {
      const result = await api('/auth/mfa/recovery-codes', {
        method: 'POST',
        body: JSON.stringify({ code: document.getElementById('mfa-manage-code').value.trim() }),
      });
      body.innerHTML = recoveryCodesBlock(result.recovery_codes, 'Done');
      wireRecoveryCodes(body, result.recovery_codes, () => renderMfaDialog());
    } catch (err) { dialogError(err.message); }
  });

  const off = document.getElementById('mfa-off');
  if (off) {
    off.onclick = (event) => guard(event.target, async () => {
      const password = prompt('Turn off two-factor authentication?\n\nConfirm your password:');
      if (!password) return;
      try {
        await api('/auth/mfa/disable', {
          method: 'POST',
          body: JSON.stringify({
            password,
            code: document.getElementById('mfa-manage-code').value.trim(),
          }),
        });
        forgetDeviceToken(currentUser?.username);  // the server deleted it too (E20)
        alert('Two-factor authentication is off.', 'success');
        await renderMfaDialog();
      } catch (err) { dialogError(err.message); }
    });
  }
}

/* E20: passkeys and trusted browsers, inside the "on" view of the dialog. */
async function renderSecondFactors(box, status) {
  let passkeys = [];
  let devices = [];
  try {
    [passkeys, devices] = await Promise.all([api('/auth/passkeys'), api('/auth/trusted-devices')]);
  } catch (err) {
    box.innerHTML = `<div class="alert alert-danger small mt-3">${esc(err.message)}</div>`;
    return;
  }
  const date = (value) => (value ? new Date(value).toLocaleDateString() : 'never');
  let passkeyPart;
  if (!status.passkeys_supported) {
    passkeyPart = `<p class="small text-secondary mb-0">Passkeys need the platform to be
      reached by a host name over HTTPS. Ask an administrator to set the public address.</p>`;
  } else if (!passkeysInBrowser()) {
    passkeyPart = '<p class="small text-secondary mb-0">This browser cannot use passkeys.</p>';
  } else {
    passkeyPart = `
      <p class="small text-secondary">A security key, or your phone or laptop's screen lock,
        can answer the second step instead of a code.</p>
      ${passkeys.length ? `<ul class="list-group list-group-flush small mb-2">
        ${passkeys.map((p) => `
          <li class="list-group-item px-0 d-flex align-items-center gap-2">
            <span class="flex-grow-1"><strong>${esc(p.name)}</strong>
              ${p.backed_up ? '<span class="badge text-bg-light ms-1">synced</span>' : ''}<br>
              <span class="text-secondary">Added ${esc(date(p.created_at))},
                last used ${esc(date(p.last_used_at))}</span></span>
            <button class="btn btn-sm btn-outline-danger" type="button"
                    data-passkey-remove="${esc(p.id)}" data-passkey-name="${esc(p.name)}"
                    >Remove</button>
          </li>`).join('')}
      </ul>` : ''}
      <form id="passkey-add-form" class="row g-2">
        <div class="col-sm-5"><input class="form-control form-control-sm" id="passkey-name"
          maxlength="64" placeholder="Name, e.g. YubiKey" required></div>
        <div class="col-sm-4"><input class="form-control form-control-sm" type="password"
          id="passkey-password" autocomplete="current-password" placeholder="Your password"
          required></div>
        <div class="col-sm-3"><button class="btn btn-sm btn-outline-primary w-100"
          type="submit">Add passkey</button></div>
      </form>`;
  }
  const devicePart = status.remember_days > 0 || devices.length ? `
    <h6 class="mt-4 mb-2">Trusted browsers</h6>
    ${devices.length ? `<ul class="list-group list-group-flush small mb-2">
      ${devices.map((d) => `
        <li class="list-group-item px-0 d-flex align-items-center gap-2">
          <span class="flex-grow-1"><strong>${esc(d.label)}</strong><br>
            <span class="text-secondary">Trusted ${esc(date(d.created_at))}, until
              ${esc(date(d.expires_at))}</span></span>
          <button class="btn btn-sm btn-outline-secondary" type="button"
                  data-device-forget="${esc(d.id)}">Forget</button>
        </li>`).join('')}
    </ul>
    <button class="btn btn-sm btn-outline-danger" type="button" id="device-forget-all"
            >Forget all</button>`
    : `<p class="small text-secondary mb-0">None. Tick "Trust this browser" at the code step
        to skip it for ${status.remember_days} day(s) on that browser.</p>`}` : '';

  box.innerHTML = `<hr><h6 class="mb-2">Passkeys</h6>${passkeyPart}${devicePart}`;

  const add = document.getElementById('passkey-add-form');
  if (add) {
    add.onsubmit = (event) => {
      event.preventDefault();
      guard(event.submitter, async () => {
        try {
          const options = await api('/auth/passkeys/options', {
            method: 'POST',
            body: JSON.stringify({ password: document.getElementById('passkey-password').value }),
          });
          let credential;
          try {
            credential = await navigator.credentials.create({
              publicKey: decodeCreationOptions(options.public_key),
            });
          } catch (err) { dialogError(passkeyBrowserError(err)); return; }
          await api('/auth/passkeys', {
            method: 'POST',
            body: JSON.stringify({
              challenge_token: options.challenge_token,
              credential: credentialJSON(credential),
              name: document.getElementById('passkey-name').value.trim(),
            }),
          });
          alert('Passkey added.', 'success');
          await renderMfaDialog();
        } catch (err) { dialogError(err.message); }
      });
    };
  }
  box.querySelectorAll('[data-passkey-remove]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      if (!confirm(`Remove the passkey "${button.dataset.passkeyName}"?`)) return;
      try {
        await api(`/auth/passkeys/${button.dataset.passkeyRemove}`, { method: 'DELETE' });
        await renderMfaDialog();
      } catch (err) { dialogError(err.message); }
    });
  });
  box.querySelectorAll('[data-device-forget]').forEach((button) => {
    button.onclick = () => guard(button, async () => {
      try {
        await api(`/auth/trusted-devices/${button.dataset.deviceForget}`, { method: 'DELETE' });
        await renderMfaDialog();
      } catch (err) { dialogError(err.message); }
    });
  });
  const all = document.getElementById('device-forget-all');
  if (all) {
    all.onclick = () => guard(all, async () => {
      if (!confirm('Forget every trusted browser? Each will ask for a code again.')) return;
      try {
        await api('/auth/trusted-devices', { method: 'DELETE' });
        forgetDeviceToken(currentUser?.username);
        await renderMfaDialog();
      } catch (err) { dialogError(err.message); }
    });
  }
}

function renderMfaConfirm(setup) {
  const body = document.getElementById('mfa-modal-body');
  body.innerHTML = `
    <p class="small">Scan this with your authenticator app, then enter the six digits it
      shows.</p>
    ${mfaSetupBlock(setup)}
    <form id="mfa-confirm-form" class="mt-3">
      <label class="form-label small text-secondary" for="mfa-confirm-code">Code</label>
      <input class="form-control font-monospace text-center fs-5" id="mfa-confirm-code"
             inputmode="numeric" autocomplete="one-time-code" maxlength="6" required>
      <div class="alert alert-danger small py-2 mt-2 d-none" id="mfa-dialog-error"></div>
      <button class="btn btn-primary mt-3 w-100" type="submit">Turn on</button>
    </form>`;
  document.getElementById('mfa-confirm-code').focus();
  document.getElementById('mfa-confirm-form').onsubmit = (event) => {
    event.preventDefault();
    guard(event.submitter, async () => {
      try {
        const result = await api('/auth/mfa/enable', {
          method: 'POST',
          body: JSON.stringify({ code: document.getElementById('mfa-confirm-code').value.trim() }),
        });
        // Every other session was signed out; this one continues on the new token.
        token.set(result.access_token);
        body.innerHTML = `<p class="small">Two-factor authentication is <strong
          class="text-success">on</strong>. Other sessions were signed out.</p>` +
          recoveryCodesBlock(result.recovery_codes, 'I have saved them');
        wireRecoveryCodes(body, result.recovery_codes, () => renderMfaDialog());
      } catch (err) { dialogError(err.message); }
    });
  };
}

function dialogError(message) {
  const box = document.getElementById('mfa-dialog-error');
  if (!box) { alert(message); return; }
  box.textContent = message;
  box.classList.remove('d-none');
}
