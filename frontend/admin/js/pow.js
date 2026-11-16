/* Sign-in check (E29): solve the proof of work `/auth/login` asks for after repeated
 * failures from this address. Shared by the console, the developer portal and the voice
 * assistant (all served from this origin, so all load /js/pow.js).
 *
 * Pure JavaScript SHA-256 on purpose: crypto.subtle exists only in secure contexts, and
 * an air-gapped site is often plain HTTP on an internal address. It is also faster here,
 * because it hashes synchronously in batches instead of awaiting every digest. */
(function () {
  'use strict';

  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const W = new Uint32Array(64);

  /** SHA-256 of an ASCII string; returns the first 32-bit word and the second. */
  function sha256Head(text) {
    const length = text.length;
    const blocks = ((length + 8) >> 6) + 1;
    const words = new Uint32Array(blocks * 16);
    for (let i = 0; i < length; i += 1) words[i >> 2] |= (text.charCodeAt(i) & 0xff) << (24 - (i % 4) * 8);
    words[length >> 2] |= 0x80 << (24 - (length % 4) * 8);
    words[blocks * 16 - 1] = length * 8;

    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
    let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    for (let block = 0; block < blocks; block += 1) {
      for (let t = 0; t < 16; t += 1) W[t] = words[block * 16 + t];
      for (let t = 16; t < 64; t += 1) {
        const a = W[t - 15], b = W[t - 2];
        const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
        const s1 = ((b >>> 17) | (b << 15)) ^ ((b >>> 19) | (b << 13)) ^ (b >>> 10);
        W[t] = (W[t - 16] + s0 + W[t - 7] + s1) | 0;
      }
      let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
      for (let t = 0; t < 64; t += 1) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (h + S1 + ch + K[t] + W[t]) | 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) | 0;
        h = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h0 = (h0 + a) | 0; h1 = (h1 + b) | 0; h2 = (h2 + c) | 0; h3 = (h3 + d) | 0;
      h4 = (h4 + e) | 0; h5 = (h5 + f) | 0; h6 = (h6 + g) | 0; h7 = (h7 + h) | 0;
    }
    return [h0 >>> 0, h1 >>> 0];
  }

  function leadingZeroBits(head) {
    if (head[0] !== 0) return Math.clz32(head[0]);
    return 32 + Math.clz32(head[1]);
  }

  /**
   * Find a solution for `{token, bits}` as sent in `error.details.challenge`.
   * Resolves to `{token, solution}` for the login body's `challenge` field. Yields to
   * the page between batches, so a spinner keeps turning.
   */
  async function solveLoginChallenge(challenge) {
    if (challenge.algorithm && challenge.algorithm !== 'sha256-leading-zero-bits') {
      throw new Error('This sign-in check is not supported by this page. Reload it.');
    }
    const prefix = `${challenge.token}:`;
    const BATCH = 20000;
    for (let counter = 0; counter < 2 ** 32; counter += BATCH) {
      for (let n = counter; n < counter + BATCH; n += 1) {
        if (leadingZeroBits(sha256Head(prefix + n)) >= challenge.bits) {
          return { token: challenge.token, solution: String(n) };
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    throw new Error('Could not complete the sign-in check.');
  }

  /**
   * Sign in through `send(challenge)`, solving the check if the server asks for one.
   * `send` performs the login request with `challenge` (null the first time) in its body
   * and throws an error carrying `code` and `details` from the server's error body.
   * `onSolving` is called before solving, so the page can say what is happening.
   */
  async function withLoginChallenge(send, onSolving) {
    try {
      return await send(null);
    } catch (err) {
      if (err?.code !== 'challenge_required' || !err.details?.challenge) throw err;
      if (onSolving) onSolving();
      return send(await solveLoginChallenge(err.details.challenge));
    }
  }

  window.solveLoginChallenge = solveLoginChallenge;
  window.withLoginChallenge = withLoginChallenge;
  window.__sha256HeadForTests = sha256Head;
})();
