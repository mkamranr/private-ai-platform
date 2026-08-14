/**
 * Microphone capture and playback (M29 §8, §16, §21).
 *
 * **Capture resamples to 16 kHz 16-bit mono in the browser**, because that is what every
 * ASR engine resamples to anyway. Doing it here means the socket carries a third of the
 * bytes a 48 kHz float stream would, and nothing in the backend has to transcode.
 *
 * An AudioWorklet does the work rather than the deprecated ScriptProcessorNode: the
 * latter runs on the main thread, so a busy render loop — which is exactly what the
 * hologram is — drops audio frames.
 *
 * The worklet is built from a Blob rather than fetched from a file. It is fifteen lines
 * that must ship with the page, and an extra network request on an air-gapped host is
 * one more thing to get wrong in nginx.
 */

/** Downsamples to 16 kHz, converts to 16-bit PCM, posts frames to the main thread. */
const WORKLET_SOURCE = `
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._target = options.processorOptions.targetRate;
    // Linear interpolation, not a proper polyphase filter: speech at 16 kHz from a
    // 48 kHz source is a 3:1 decimation, and the aliasing it lets through sits above
    // what the models care about. A real filter belongs here if quality ever matters.
    this._ratio = sampleRate / this._target;
    this._carry = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];

    const out = new Int16Array(Math.floor((channel.length - this._carry) / this._ratio) + 1);
    let count = 0;
    let index = this._carry;
    while (index < channel.length) {
      const sample = channel[Math.floor(index)];
      // Clamp before scaling: a sample above 1.0 wraps to a loud negative otherwise,
      // which is heard as a click.
      const clamped = Math.max(-1, Math.min(1, sample));
      out[count] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      count += 1;
      index += this._ratio;
    }
    this._carry = index - channel.length;

    if (count > 0) {
      const frame = out.buffer.slice(0, count * 2);
      this.port.postMessage(frame, [frame]);
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);
`;

export class AudioInput {
  /**
   * @param {number} targetRate sample rate the backend expects
   * @param {(pcm: ArrayBuffer) => void} onFrame called per captured frame
   * @param {(level: number, spectrum: Uint8Array) => void} onLevel amplitude and spectrum
   */
  constructor(targetRate, onFrame, onLevel) {
    this._targetRate = targetRate;
    this._onFrame = onFrame;
    this._onLevel = onLevel;
    this._context = null;
    this._stream = null;
    this._analyser = null;
    this._levelTimer = null;
  }

  get active() {
    return Boolean(this._stream);
  }

  async start() {
    if (this._stream) return;
    // The browser's own processing, left on deliberately: echo cancellation is what
    // stops the assistant's own voice being transcribed as the user interrupting it.
    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });

    this._context = new AudioContext();
    const source = this._context.createMediaStreamSource(this._stream);

    const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await this._context.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    const worklet = new AudioWorkletNode(this._context, 'capture-processor', {
      processorOptions: { targetRate: this._targetRate },
    });
    worklet.port.onmessage = (event) => this._onFrame(event.data);

    // A parallel analyser drives the avatar. Reading amplitude from the worklet frames
    // would tie the visuals to the network path — they would stutter whenever the socket
    // did, which is precisely when the user most needs to see that it is still listening.
    this._analyser = this._context.createAnalyser();
    this._analyser.fftSize = 256;
    source.connect(this._analyser);
    source.connect(worklet);
    // Not connected to the destination: routing the microphone to the speakers is
    // feedback, not monitoring.

    this._startLevelPolling();
  }

  stop() {
    // Cleared before the analyser goes, or the next tick reads a disconnected node.
    if (this._levelTimer) clearInterval(this._levelTimer);
    this._levelTimer = null;
    this._stream?.getTracks().forEach((track) => track.stop());
    this._stream = null;
    this._analyser = null;
    this._context?.close();
    this._context = null;
    this._onLevel(0);
  }

  _startLevelPolling() {
    this._levelTimer = pollAnalyser(this._analyser, this._onLevel);
  }
}

/**
 * Report amplitude and spectrum from an analyser, until cleared.
 *
 * **Both are sent, because different avatars need different things.** The hologram theme
 * wants one number to drive a jaw; the waveform theme draws the spectrum itself, and
 * given only an amplitude it would have to invent a shape — a sine wave scaled by
 * loudness, which wobbles convincingly while showing nothing about what was said.
 *
 * 30 Hz rather than 60: the avatar smooths the amplitude and the spectrum only drives a
 * slow-moving envelope, so a faster poll costs main-thread time and buys nothing.
 *
 * @returns the interval handle, to be cleared by the caller
 */
function pollAnalyser(analyser, onLevel) {
  const wave = new Uint8Array(analyser.frequencyBinCount);
  const spectrum = new Uint8Array(analyser.frequencyBinCount);
  return setInterval(() => {
    analyser.getByteTimeDomainData(wave);
    analyser.getByteFrequencyData(spectrum);
    // RMS around the 128 midpoint, scaled so ordinary speech lands near the middle of the
    // range rather than pinning the avatar at full brightness.
    let sum = 0;
    for (let i = 0; i < wave.length; i += 1) {
      const deviation = (wave[i] - 128) / 128;
      sum += deviation * deviation;
    }
    onLevel(Math.min(1, Math.sqrt(sum / wave.length) * 3.2), spectrum);
  }, 33);
}

/**
 * Plays WAV chunks streamed from the server, and stops instantly when interrupted.
 *
 * Chunks are concatenated and decoded once at AUDIO_END rather than played as they
 * arrive: the platform's TTS synthesises a whole utterance, so the frames are pieces of
 * one WAV file and no piece after the first is independently decodable. When the backend
 * grows sentence-level streaming (§22), this is the class that changes.
 */
export class AudioOutput {
  constructor(onLevel) {
    this._onLevel = onLevel;
    this._chunks = [];
    this._context = null;
    this._source = null;
    this._analyser = null;
    this._timer = null;
  }

  begin() {
    this._chunks = [];
  }

  push(buffer) {
    this._chunks.push(buffer);
  }

  /** Decode and play what was collected. Resolves when playback finishes or is stopped. */
  async play() {
    if (!this._chunks.length) return;
    const total = this._chunks.reduce((sum, c) => sum + c.byteLength, 0);
    const merged = new Uint8Array(total);
    let offset = 0;
    for (const chunk of this._chunks) {
      merged.set(new Uint8Array(chunk), offset);
      offset += chunk.byteLength;
    }
    this._chunks = [];

    this._context = this._context || new AudioContext();
    let decoded;
    try {
      decoded = await this._context.decodeAudioData(merged.buffer);
    } catch {
      // Undecodable audio is not fatal: the transcript is already on screen, which is
      // the degradation §39 asks for — speech-to-speech falls back to speech-to-text.
      return;
    }

    return new Promise((resolve) => {
      this._source = this._context.createBufferSource();
      this._source.buffer = decoded;
      this._analyser = this._context.createAnalyser();
      this._analyser.fftSize = 256;
      this._source.connect(this._analyser);
      this._analyser.connect(this._context.destination);
      this._source.onended = () => {
        this._stopLevelPolling();
        resolve();
      };
      this._startLevelPolling();
      this._source.start();
    });
  }

  /** Barge-in: cut the audio now (§23). */
  stop() {
    this._chunks = [];
    this._stopLevelPolling();
    if (this._source) {
      try {
        this._source.onended = null;
        this._source.stop();
      } catch { /* already finished */ }
      this._source = null;
    }
  }

  _startLevelPolling() {
    this._timer = pollAnalyser(this._analyser, this._onLevel);
  }

  _stopLevelPolling() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
    this._onLevel(0);
  }
}
