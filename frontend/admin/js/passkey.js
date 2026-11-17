/**
 * Passkeys (WebAuthn) in the browser, shared by the console, the developer portal and the
 * voice assistant (E20, E30, E33). A classic script: everything here is a global.
 *
 * The server sends WebAuthn options as JSON (base64url for binary fields) and expects the
 * credential back the same way; these helpers convert both directions, using the
 * browser's own parse/toJSON where it has them.
 *
 * The two sign-in flows take ``post(path, body)``, each page's own JSON POST that throws
 * an Error carrying the server's message, so the pages keep their own error handling.
 */

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

/** E30: sign in with a passkey alone. No username: the passkey chosen names the account.
 *  Resolves to the server's token pair. */
async function passkeySignIn(post) {
  const options = await post('/auth/passkey/options', {});
  let credential;
  try {
    credential = await navigator.credentials.get({
      publicKey: decodeRequestOptions(options.public_key),
    });
  } catch (err) { throw new Error(passkeyBrowserError(err)); }
  return post('/auth/passkey/verify', {
    challenge_token: options.challenge_token, credential: credentialJSON(credential),
  });
}

/** E20: answer the second sign-in step with a passkey instead of a code. */
async function passkeyMfaAnswer(post, mfaToken, extra = {}) {
  const options = await post('/auth/mfa/passkey/options', { mfa_token: mfaToken });
  let credential;
  try {
    credential = await navigator.credentials.get({
      publicKey: decodeRequestOptions(options.public_key),
    });
  } catch (err) { throw new Error(passkeyBrowserError(err)); }
  return post('/auth/mfa/passkey/verify', {
    mfa_token: mfaToken,
    challenge_token: options.challenge_token,
    credential: credentialJSON(credential),
    ...extra,
  });
}
