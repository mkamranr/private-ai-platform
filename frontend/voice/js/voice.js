/**
 * The voice assistant page (M29).
 *
 * Owns the session: sign in, open a session over REST, hold the WebSocket, and map the
 * server's events onto the avatar, the transcript and the tool cards.
 *
 * **The server's state is the state.** The UI never guesses what the assistant is doing
 * from what it just sent — it renders what the server said it is doing. A client that
 * predicts goes out of sync exactly when a turn goes wrong, which is when the person is
 * already confused.
 *
 * **Nothing sensitive is rendered.** Tool cards show a name and an outcome, because that
 * is all the server sends (§11). There is nowhere in this file that could display a
 * credential, because the credential never leaves the backend.
 */

import { AudioInput, AudioOutput } from './audio.js';
import { HologramAvatar } from './hologram.js';
import { WaveformAvatar } from './waveface.js';

const API = '/api/v1';
const TOKEN_KEY = 'aip.token';
const THEME_KEY = 'aip.voice.avatar';

/**
 * The avatar themes, and the only place that knows more than one exists.
 *
 * Both classes expose the same handful of methods, so everything downstream — the state
 * machine, the audio callbacks, the resize on reveal — is written once and neither theme
 * is a special case.
 */
const THEMES = { neural: HologramAvatar, waveform: WaveformAvatar };
const DEFAULT_THEME = 'neural';

/** Server event → what the avatar and the caption should show (§38). */
const STATE_TEXT = {
  idle: 'Ready',
  listening: "I'm listening",
  thinking: 'Thinking…',
  tool: 'Checking…',
  speaking: 'Speaking',
  error: 'Something went wrong',
};

const token = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (value) => sessionStorage.setItem(TOKEN_KEY, value),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

const el = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');

class VoiceAssistant {
  constructor() {
    this.avatar = null;
    this.theme = null;
    this.stateName = 'idle';
    this.setTheme(localStorage.getItem(THEME_KEY) || DEFAULT_THEME);
    this.input = new AudioInput(16000, (pcm) => this._sendAudio(pcm),
      (level, spectrum) => this.avatar.setAudioLevel(level, spectrum));
    // The arrow reads `this.avatar` when the audio fires, not when this line runs, so
    // swapping the theme mid-session does not leave playback driving a discarded avatar.
    this.output = new AudioOutput((level, spectrum) => this.avatar.setAudioLevel(level, spectrum));
    this.socket = null;
    this.session = null;
    this.config = null;
    this.holding = false;
  }

  /**
   * Swap the avatar, keeping whatever the assistant is currently doing.
   *
   * **The canvas is replaced, not reused.** A disposed renderer leaves its WebGL context
   * bound to the element it was built on, and a second renderer over the top of that
   * inherits the first one's state — which shows up as a black rectangle the second time
   * you change theme, not the first.
   */
  setTheme(name) {
    const theme = THEMES[name] ? name : DEFAULT_THEME;
    if (theme === this.theme) return;

    this.avatar?.dispose();
    const previous = el('hologram');
    const canvas = document.createElement('canvas');
    canvas.id = 'hologram';
    canvas.setAttribute('aria-hidden', 'true');
    previous.replaceWith(canvas);
    // A WebGL failure note from the outgoing theme would otherwise stack up.
    canvas.parentElement.querySelector('.no-webgl')?.remove();

    this.avatar = new THEMES[theme](canvas);
    this.avatar.setState(this.stateName);
    this.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
    for (const button of document.querySelectorAll('.seg')) {
      button.setAttribute('aria-pressed', String(button.dataset.theme === theme));
    }
  }

  // -- lifecycle -----------------------------------------------------------
  async signIn(username, password) {
    const response = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!response.ok) throw new Error('Sign-in failed. Check the username and password.');
    const body = await response.json();
    token.set(body.access_token);
  }

  async load() {
    this.config = await this._api('/voice/config');
    if (!this.config.enabled) {
      throw new Error(
        'The voice assistant is switched off. An administrator can enable it under '
        + 'Administration → Voice Assistant in the admin console.',
      );
    }
    // The agent is shown, not chosen. Fetched only to turn the configured slug into a
    // name somebody would recognise; if that lookup fails the slug still reads fine, and
    // a name in the corner is not worth failing a sign-in over.
    let name = this.config.default_agent_slug || 'Assistant';
    try {
      const agents = await this._api('/agents');
      const match = (agents.items || agents)
        .find((a) => a.slug === this.config.default_agent_slug);
      if (match) name = match.display_name;
    } catch { /* the chip falls back to the slug */ }
    el('agent-name').textContent = name;
    el('agent-chip').classList.remove('d-none');
  }

  async openSession() {
    // Neither the agent nor the language is sent. The server applies the site defaults
    // it already holds — and the language default is auto-detect, which is the right
    // answer often enough that asking would mostly be a chance to get it wrong.
    this.session = await this._api('/voice/sessions', {
      method: 'POST',
      body: JSON.stringify({}),
    });

    // The token goes in the query string because a browser cannot set a header on a
    // WebSocket upgrade. It is verified before the socket is accepted.
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${scheme}://${location.host}${this.session.websocket_url}?token=${encodeURIComponent(token.get())}`;
    this.socket = new WebSocket(url);
    this.socket.binaryType = 'arraybuffer';

    this.socket.onmessage = (event) => this._onMessage(event);
    this.socket.onclose = () => this._setState('idle', 'Session ended');
    this.socket.onerror = () => this._setState('error', 'Connection lost');

    await new Promise((resolve, reject) => {
      this.socket.onopen = resolve;
      setTimeout(() => reject(new Error('The voice service did not respond.')), 10000);
    });
  }

  // -- talking -------------------------------------------------------------
  async startTalking() {
    if (this.holding) return;
    this.holding = true;

    // Barge-in: pressing the microphone while the assistant is speaking interrupts it,
    // which is the behaviour people expect from a conversation (§23).
    if (this.avatar.state === 'speaking') {
      this.output.stop();
      this._send({ type: 'INTERRUPT' });
    }

    try {
      await this.input.start();
    } catch {
      this._setState('error', 'No microphone. Check the browser permission.');
      this.holding = false;
      return;
    }
    this._send({ type: 'AUDIO_START' });
    this._setState('listening');
    el('mic').classList.add('listening');
  }

  stopTalking() {
    if (!this.holding) return;
    this.holding = false;
    el('mic').classList.remove('listening');
    this.input.stop();
    this._send({ type: 'AUDIO_END' });
    this._setState('thinking');
  }

  // -- server events -------------------------------------------------------
  _onMessage(event) {
    if (event.data instanceof ArrayBuffer) {
      this.output.push(event.data);
      return;
    }
    const message = JSON.parse(event.data);
    switch (message.type) {
      case 'SESSION_STARTED':
        this._setState('idle', 'Ready — hold the microphone and speak');
        break;
      case 'LISTENING_STARTED':
        this._setState('listening');
        break;
      case 'TRANSCRIPT_PARTIAL':
        this._showPartial(message.text);
        break;
      case 'TRANSCRIPT_FINAL':
        this._addTurn('you', message.text, message.language);
        this._setState('thinking');
        break;
      case 'AGENT_STARTED':
        this._setState('thinking');
        break;
      case 'TOOL_STARTED':
        this._setState('tool', `Using ${message.tool}…`);
        this._toolCard(message.tool, 'running');
        break;
      case 'TOOL_COMPLETED':
        this._toolCard(message.tool, message.success ? 'done' : 'failed');
        break;
      case 'APPROVAL_REQUIRED':
        // Voice does not get to approve a high-risk action by itself (§42-43). The
        // person confirms in the admin console, where the action is spelled out.
        this._setState('tool', 'Waiting for approval');
        this._addNotice(
          `“${esc(message.tool)}” needs approval before it can run. `
          + 'Approve it under Agents → Approvals in the admin console.',
        );
        break;
      case 'RESPONSE_TEXT_FINAL':
        this._addTurn('assistant', message.text);
        break;
      case 'AUDIO_START':
        this.output.begin();
        this._setState('speaking');
        break;
      case 'AUDIO_END':
        this.output.play().then(() => {
          if (this.avatar.state === 'speaking') this._setState('idle', 'Ready');
        });
        break;
      case 'AUDIO_UNAVAILABLE':
        // Degraded, not broken: the answer is already on screen (§39).
        this._addNotice('Speech synthesis is unavailable — showing the answer as text.');
        this._setState('idle', 'Ready');
        break;
      case 'ASSISTANT_INTERRUPTED':
        this.output.stop();
        this._setState('listening', 'Go ahead');
        break;
      case 'ERROR':
        this._setState('error', message.message);
        break;
      default:
        break;
    }
  }

  // -- rendering -----------------------------------------------------------
  _setState(state, caption) {
    this.stateName = state;
    this.avatar.setState(state);
    el('caption').textContent = caption || STATE_TEXT[state] || '';
    el('caption').className = `caption caption-${state}`;
    el('agent-chip').dataset.state = state;
  }

  _showPartial(text) {
    el('partial').textContent = text || '';
  }

  _addTurn(role, text, language) {
    el('partial').textContent = '';
    el('transcript-empty').classList.add('d-none');
    const transcript = el('transcript');
    const turn = document.createElement('div');
    turn.className = `turn turn-${role}`;
    turn.innerHTML = `<div class="turn-role">${role === 'you' ? 'You' : 'Assistant'}</div>
      <div class="turn-text" ${language === 'ar' ? 'dir="rtl"' : ''}>${esc(text)}</div>`;
    transcript.appendChild(turn);
    transcript.scrollTop = transcript.scrollHeight;
  }

  _addNotice(html) {
    el('transcript-empty').classList.add('d-none');
    const transcript = el('transcript');
    const notice = document.createElement('div');
    notice.className = 'notice';
    notice.innerHTML = html;
    transcript.appendChild(notice);
    transcript.scrollTop = transcript.scrollHeight;
  }

  /** One card per tool, updated in place rather than appended twice (§11, §20). */
  _toolCard(tool, status) {
    const id = `tool-${String(tool).replace(/[^a-z0-9_-]/gi, '')}`;
    let card = document.getElementById(id);
    if (!card) {
      card = document.createElement('div');
      card.id = id;
      card.className = 'tool-card';
      el('tools').appendChild(card);
    }
    const icon = { running: '⟳', done: '✓', failed: '⚠' }[status];
    card.className = `tool-card tool-${status}`;
    card.innerHTML = `<span class="tool-icon">${icon}</span> <span class="tool-name">${esc(tool)}</span>`;
    if (status !== 'running') {
      // Cards fade out after the turn: they are progress, not history. The transcript is
      // where the conversation lives (§20).
      setTimeout(() => card.remove(), 6000);
    }
  }

  // -- plumbing ------------------------------------------------------------
  _send(message) {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(message));
    }
  }

  _sendAudio(pcm) {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(pcm);
  }

  async _api(path, options = {}) {
    const response = await fetch(`${API}${path}`, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token.get()}`,
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = await response.json();
        message = body?.error?.message || body?.detail || message;
      } catch { /* non-JSON error body */ }
      throw new Error(message);
    }
    return response.json();
  }
}

/* -------------------------------------------------------------------------- */
/* Wiring                                                                      */
/* -------------------------------------------------------------------------- */
const assistant = new VoiceAssistant();

function showError(message) {
  el('error').textContent = message;
  el('error').classList.remove('d-none');
}

async function enter() {
  el('error').classList.add('d-none');
  try {
    await assistant.load();
    await assistant.openSession();
    el('gate').classList.add('d-none');
    el('stage').classList.remove('d-none');
    // The avatar was built while this was `display: none`, so it has never had a size.
    assistant.avatar.resize();
  } catch (err) {
    showError(err.message);
  }
}

for (const button of document.querySelectorAll('.seg')) {
  button.onclick = () => {
    assistant.setTheme(button.dataset.theme);
    // The new canvas has never been measured; the ResizeObserver fires on observe, but
    // only once the element is actually in the layout.
    assistant.avatar.resize();
  };
}

el('signin-form').onsubmit = async (event) => {
  event.preventDefault();
  el('error').classList.add('d-none');
  try {
    await assistant.signIn(el('username').value, el('password').value);
    await enter();
  } catch (err) {
    showError(err.message);
  }
};

// Press and hold, on mouse and on touch. `pointerdown` covers both, and the listener on
// the window catches a release that happens after the pointer has left the button —
// otherwise letting go off-target leaves the microphone open.
el('mic').addEventListener('pointerdown', (event) => {
  event.preventDefault();
  assistant.startTalking();
});
window.addEventListener('pointerup', () => assistant.stopTalking());

// Space to talk, because a hand on the keyboard is the common case at a desk. Ignored
// while typing, and `repeat` is dropped so holding it does not restart the turn.
window.addEventListener('keydown', (event) => {
  if (event.code !== 'Space' || event.repeat) return;
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
  event.preventDefault();
  assistant.startTalking();
});
window.addEventListener('keyup', (event) => {
  if (event.code === 'Space') assistant.stopTalking();
});

// An existing session token skips the sign-in gate, so opening this page from the admin
// console does not ask for a password that was already given.
if (token.get()) {
  enter().catch(() => {
    el('gate').classList.remove('d-none');
  });
} else {
  el('gate').classList.remove('d-none');
}
