/**
 * The "Waveform" avatar theme: a still head, and a voice you can see.
 *
 * The other theme (`hologram.js`) animates the face — the jaw swings with the audio. This
 * one does the opposite and puts **every bit of motion into a single band of light** that
 * crosses the head at eye level. The head itself never moves.
 *
 * That trade is the point rather than a shortcut. A lip-synced face has to be *right* to
 * be convincing, and a face that is nearly right is worse than no face at all; a waveform
 * is legible at a glance, honest about what it is showing, and reads at any size. It also
 * separates the two speakers cleanly, which is what this theme is for:
 *
 *     you speaking   -> cyan
 *     assistant      -> violet
 *
 * **The wave is the audio, not an animation of it.** Its shape comes from the frequency
 * spectrum of whichever stream is live (`setAudioLevel`), mirrored about the centre, so
 * low frequencies sit in the middle and sibilants flick out towards the edges. At rest it
 * settles to a flat line. Nothing here runs on a timer that would keep waving after the
 * sound stopped.
 *
 * The head is a scatter of nodes over the shared shell in `head.js`, wired to its
 * neighbours — irregular rather than a grid, which is what distinguishes it at a glance
 * from the other theme. It keeps its **ears** and nothing else: no eyes, no mouth. A
 * featureless shell with ears reads as a head; without them it reads as an egg.
 */

import * as THREE from '../vendor/three.module.min.js';
import {
  BROW_Y, CHIN_Y, CROWN_Y, EYE_X, EYE_Y, MAX_TURN, NOSE_BASE_Y,
  headHalfDepth, headHalfWidth, sculptFace, surfaceZ,
} from './head.js';
import { makeGlowSprite, writeSegments } from './glow.js';

/**
 * State → palette.
 *
 * `listening` and `speaking` are deliberately far apart on the colour wheel — cyan and
 * violet — because telling those two apart at a glance is the whole reason this theme
 * exists. The other theme's palette has them both in cyan, which is fine there because
 * the mouth is what carries the state.
 */
const PALETTE = {
  idle:      { node: 0x4f7fc4, wave: 0x2f6fed, core: 0x8fb6ff, intensity: 0.4 },
  listening: { node: 0x7fe9ff, wave: 0x22d3ee, core: 0xe6fdff, intensity: 1.0 },
  thinking:  { node: 0xffd27a, wave: 0xf59e0b, core: 0xfff2d4, intensity: 0.75 },
  tool:      { node: 0xffc94d, wave: 0xd97706, core: 0xffeab0, intensity: 0.85 },
  speaking:  { node: 0xc4a0ff, wave: 0x9d5cff, core: 0xf3e8ff, intensity: 1.0 },
  error:     { node: 0xff9d9d, wave: 0xef4444, core: 0xffe0e0, intensity: 0.7 },
};

const ROWS = 46;             // scatter rows down the head
const COLUMNS = 32;          // scatter samples across the widest row
const JITTER = 0.55;         // how far a node strays from its grid slot, as a fraction
const KEEP = 0.72;           // proportion of slots that get a node, for an uneven field
// Short, because the field is dense. Too long and the web stops following the surface
// and starts drawing chords straight across the face.
const LINK_RADIUS = 0.175;
const LINKS_PER_NODE = 3;

/** Ears run from the brow to the base of the nose. The canon again. */
const EAR_Y = (BROW_Y + NOSE_BASE_Y) / 2;

// Filament 0 is the **spine**: a near-flat, bright line running the whole width. The
// reference has one and it is doing a lot of work — without it a bundle of equal
// filaments reads as a smear, and with it the smear becomes a wave around a line.
const STRANDS = 8;
const WAVE_POINTS = 200;     // samples along each filament
const WAVE_SPAN = 2.7;       // half-width; it runs off both sides of the frame, as in the
                             // reference, so the band reads as passing through the head
                             // rather than as an object sitting inside it
const WAVE_Z = 1.55;         // in front of the nose tip, which reaches about 1.39

export class WaveformAvatar {
  constructor(canvas) {
    this._canvas = canvas;
    this._state = 'idle';
    this._level = 0;
    this._targetLevel = 0;
    // The spectrum of whatever is currently making sound. Starts flat, so the first frame
    // renders a resting line rather than reading an undefined array.
    this._spectrum = new Uint8Array(64);
    this._clock = new THREE.Clock();
    this._reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._failed = false;

    this._scene = new THREE.Scene();
    this._camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
    // Framed so the head sits in the upper three-quarters: the caption and the tool chips
    // overlay the bottom of this viewport, and a head filling the frame puts them across
    // its chin. Both themes use the same camera, or switching would jog the head.
    this._camera.position.set(0, -0.40, 5.6);

    try {
      this._renderer = new THREE.WebGLRenderer({
        canvas,
        antialias: true,
        alpha: true,
        premultipliedAlpha: false,
      });
    } catch {
      this._failed = true;
      this._showFallback();
      return;
    }
    this._renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    this._group = new THREE.Group();
    this._scene.add(this._group);

    this._sprite = makeGlowSprite();
    this._buildHead();
    this._buildLinks();
    this._buildWave();
    this._buildCore();
    this._buildAmbient();
    this._applyPalette(PALETTE.idle);

    this._observeSize();
    this._renderer.setAnimationLoop(() => this._frame());
  }

  // -- construction --------------------------------------------------------

  /**
   * Scatter nodes over the front of the head.
   *
   * A **jittered grid**, not a Poisson disc: each slot of a regular grid is nudged by up
   * to half a cell and then kept or dropped at random. It gives the irregular web of the
   * reference for none of the cost of thinning a few thousand candidates against each
   * other, and unlike true randomness it cannot leave a bald patch.
   */
  _buildHead() {
    const base = [];
    const accent = [];
    const put = (x, y, z, glow = 0) => {
      base.push(x, y, z);
      accent.push(glow);
      return accent.length - 1;
    };

    const rowSpan = (CROWN_Y - CHIN_Y) / ROWS;
    const uMax = MAX_TURN + Math.PI / 2;
    for (let row = 0; row < ROWS; row += 1) {
      const slotY = CROWN_Y - rowSpan * (row + 0.5);
      const columns = Math.max(6, Math.round(COLUMNS * headHalfWidth(slotY)));
      for (let column = 0; column < columns; column += 1) {
        if (Math.random() > KEEP) continue;

        const y = slotY + (Math.random() - 0.5) * rowSpan * JITTER * 2;
        const halfWidth = headHalfWidth(y);
        if (halfWidth < 0.03) continue;
        const slotU = -uMax + ((column + 0.5) / columns) * uMax * 2;
        const u = slotU + (Math.random() - 0.5) * (uMax * 2 / columns) * JITTER * 2;

        const across = Math.cos(u);
        const x = halfWidth * Math.sin(u);
        const z = headHalfDepth(y) * across + sculptFace(x, y) * Math.max(0, across);
        // Brighter towards the silhouette, which is what gives a featureless shell its
        // volume — there is no relief to shade here, only the turn of the surface.
        put(x, y, z, Math.abs(Math.sin(u)) ** 4 * 0.5);
      }
    }

    // The outline, dense and bright. Same reasoning as the other theme: a head is read
    // from its edge first, and a scatter alone leaves that edge ragged.
    for (const side of [-1, 1]) {
      for (let i = 0; i <= 60; i += 1) {
        const y = CROWN_Y - (CROWN_Y - CHIN_Y) * (i / 60);
        const x = side * headHalfWidth(y);
        put(x, y, surfaceZ(x, y), 0.85);
      }
    }

    this._addEars(put);

    this._base = new Float32Array(base);
    this._accent = Float32Array.from(accent);
    this._count = accent.length;
    // A phase per node: the field twinkles rather than pulsing as one object. Brightness
    // only — the head itself holds still, which is the premise of this theme.
    this._phase = new Float32Array(this._count);
    for (let i = 0; i < this._count; i += 1) this._phase[i] = Math.random() * Math.PI * 2;

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(this._base), 3));
    this._colors = new Float32Array(this._count * 3);
    geometry.setAttribute('color', new THREE.BufferAttribute(this._colors, 3));
    this._nodeMaterial = new THREE.PointsMaterial({
      size: 0.045,
      map: this._sprite,
      vertexColors: true,
      transparent: true,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
    });
    this._nodes = new THREE.Points(geometry, this._nodeMaterial);
    this._group.add(this._nodes);
  }

  _addEars(put) {
    // The only feature this theme keeps. A shell with no eyes and no mouth reads as an
    // egg; the same shell with ears reads as a head, and it costs forty nodes.
    for (const side of [-1, 1]) {
      for (let i = 0; i <= 17; i += 1) {
        const angle = -Math.PI * 0.5 + (i / 17) * Math.PI;   // lobe -> rim -> top
        const y = EAR_Y + Math.sin(angle) * 0.30;
        const x = side * (headHalfWidth(y) * 0.93 + Math.cos(angle) * 0.17);
        put(x, y, 0.05, 0.8);
      }
      for (let i = 0; i <= 6; i += 1) {                       // the inner fold
        const angle = -Math.PI * 0.45 + (i / 6) * Math.PI * 0.9;
        const y = EAR_Y + Math.sin(angle) * 0.16;
        const x = side * (headHalfWidth(y) * 0.95 + Math.cos(angle) * 0.07);
        put(x, y, 0.14, 0.45);
      }
    }
  }

  /**
   * Wire each node to its nearest few.
   *
   * Proximity is right here, where it was wrong for the grid theme: these nodes have no
   * row-and-column topology to follow, and the uneven web it produces is the look.
   */
  _buildLinks() {
    const pairs = [];
    const radiusSquared = LINK_RADIUS * LINK_RADIUS;
    for (let a = 0; a < this._count; a += 1) {
      let linked = 0;
      for (let b = a + 1; b < this._count && linked < LINKS_PER_NODE; b += 1) {
        const dx = this._base[a * 3] - this._base[b * 3];
        const dy = this._base[a * 3 + 1] - this._base[b * 3 + 1];
        const dz = this._base[a * 3 + 2] - this._base[b * 3 + 2];
        if (dx * dx + dy * dy + dz * dz > radiusSquared) continue;
        pairs.push(a, b);
        linked += 1;
      }
    }

    this._pairs = Uint16Array.from(pairs);
    this._linkPositions = new Float32Array((this._pairs.length / 2) * 6);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(this._linkPositions, 3));
    this._linkMaterial = new THREE.LineBasicMaterial({
      transparent: true,
      opacity: 0.22,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._links = new THREE.LineSegments(geometry, this._linkMaterial);
    this._group.add(this._links);
    // The head never moves, so unlike the other theme this is written once and left.
    writeSegments(this._pairs, this._base, this._linkPositions);
  }

  /** The ribbon: several filaments sharing one envelope, each on its own phase. */
  _buildWave() {
    this._strands = [];
    for (let s = 0; s < STRANDS; s += 1) {
      const positions = new Float32Array((WAVE_POINTS + 1) * 3);
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      const material = new THREE.LineBasicMaterial({
        transparent: true,
        // The middle filaments carry the line; the outer ones are a haze around it.
        opacity: s === 0 ? 0.95 : 0.5 - Math.abs(s - STRANDS / 2) * 0.07,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const line = new THREE.Line(geometry, material);
      this._scene.add(line);
      this._strands.push({ line, positions, material });
    }
  }

  /**
   * The hot core, and a glow where the band crosses each eye socket.
   *
   * Sprites rather than points: a sprite can be scaled individually, and the whole effect
   * is the centre swelling with the voice while the flanks hold steadier.
   */
  _buildCore() {
    this._cores = [];
    for (const [x, size] of [[0, 0.62], [-EYE_X, 0.34], [EYE_X, 0.34]]) {
      const material = new THREE.SpriteMaterial({
        map: this._sprite,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const sprite = new THREE.Sprite(material);
      sprite.position.set(x, EYE_Y, WAVE_Z - 0.05);
      this._scene.add(sprite);
      this._cores.push({ sprite, material, size });
    }
  }

  /** A sparse field behind the head, so it sits in space rather than on a flat void. */
  _buildAmbient() {
    const count = 460;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const radius = 2.6 + Math.random() * 1.8;
      positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = radius * Math.cos(phi) * 0.8;
      positions[i * 3 + 2] = radius * Math.sin(phi) * Math.sin(theta) - 0.9;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    this._ambientMaterial = new THREE.PointsMaterial({
      size: 0.045,
      map: this._sprite,
      transparent: true,
      opacity: 0.35,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._ambient = new THREE.Points(geometry, this._ambientMaterial);
    this._scene.add(this._ambient);
  }

  // -- public API, matching HologramAvatar so the themes are interchangeable ----
  setState(state) {
    if (!PALETTE[state] || state === this._state) return;
    this._state = state;
    if (!this._failed) this._applyPalette(PALETTE[state]);
  }

  get state() {
    return this._state;
  }

  /**
   * @param {number} level 0..1 amplitude
   * @param {Uint8Array} [spectrum] frequency bins, 0..255, if the caller has them
   */
  setAudioLevel(level, spectrum) {
    this._targetLevel = Math.max(0, Math.min(1, level || 0));
    if (spectrum && spectrum.length) this._spectrum = spectrum;
  }

  resize() {
    if (!this._failed) this._onResize();
  }

  reset() {
    this.setState('idle');
    this.setAudioLevel(0);
  }

  dispose() {
    this._observer?.disconnect();
    this._renderer?.setAnimationLoop(null);
    this._renderer?.dispose();
  }

  // -- internals -----------------------------------------------------------
  _applyPalette(palette) {
    this._nodeColor = new THREE.Color(palette.node);
    this._linkMaterial.color.setHex(palette.node);
    this._ambientMaterial.color.setHex(palette.node);
    for (const strand of this._strands) strand.material.color.setHex(palette.wave);
    for (const core of this._cores) core.material.color.setHex(palette.core);
    this._intensity = palette.intensity;
  }

  _frame() {
    const elapsed = this._clock.getElapsedTime();
    this._level += (this._targetLevel - this._level) * 0.18;

    this._updateWave(elapsed);
    this._shade(elapsed);

    for (const core of this._cores) {
      core.sprite.scale.setScalar(core.size * (0.45 + this._level * 1.25));
      core.material.opacity = (0.1 + this._level * 0.55) * this._intensity;
    }

    this._linkMaterial.opacity = 0.08 + this._intensity * 0.11 + this._level * 0.12;
    this._ambientMaterial.opacity = 0.2 + this._level * 0.25;
    if (!this._reduced) this._ambient.rotation.y -= 0.0009;

    this._renderer.render(this._scene, this._camera);
  }

  /**
   * Redraw the ribbon from the current spectrum.
   *
   * Position along the band maps to frequency, mirrored about the centre: bass in the
   * middle, treble at the tips. That is why a vowel makes it swell in the centre and an
   * "s" makes the ends flicker — the shape is the voice, not a loop that happens to be
   * playing while the voice does.
   *
   * The spectrum is polled a few dozen times a second while frames run at sixty, so what
   * it drives is the **envelope**, which changes slowly. The filaments' own oscillation is
   * continuous, so nothing steps.
   */
  _updateWave(elapsed) {
    const bins = this._spectrum.length;
    const time = this._reduced ? 0 : elapsed;

    for (let s = 0; s < STRANDS; s += 1) {
      const { positions, line } = this._strands[s];
      const spread = s - (STRANDS - 1) / 2;

      for (let i = 0; i <= WAVE_POINTS; i += 1) {
        const t = i / WAVE_POINTS;
        const u = (t - 0.5) * 2;                       // -1 at the left tip, +1 at right
        const distance = Math.abs(u);

        // Frequency at this position. Curved, so the bass end — where nearly all of the
        // energy in speech is — gets the middle half of the band instead of a sliver.
        const energy = this._spectrum[Math.min(bins - 1, Math.floor(distance ** 1.7 * bins))] / 255;
        // Fat in the middle, pinned to nothing at both tips.
        const envelope = (1 - distance ** 3) * Math.exp(-distance * distance * 1.5);
        // Scaled by the *smoothed* amplitude, not added to it. Polling stops when the
        // stream does, so the last spectrum read is whatever happened to be in the air at
        // the final tick — left to drive the shape on its own it would hold that pose for
        // ever. Gated on a level that decays, the band settles to a flat line instead.
        const amplitude = envelope * (0.035 + this._level * (0.25 + energy * 0.9));

        positions[i * 3] = u * WAVE_SPAN;
        // Frequency rises with the envelope, so the middle is a tighter burst that opens
        // out into long slow swells at the tips. Kept well under WAVE_POINTS/4 cycles: at
        // more than that the line aliases and the burst renders as vertical hatching.
        const rate = (3.5 + s * 0.8) * (1 + 1.1 * envelope);
        // The spine barely moves. Everything else swings around it.
        const swing = amplitude * (s === 0 ? 0.06 : 0.42);
        positions[i * 3 + 1] = EYE_Y
          + Math.sin(u * Math.PI * rate + time * (2.2 + s * 0.3) + s) * swing
          + spread * envelope * 0.012;
        positions[i * 3 + 2] = WAVE_Z
          + Math.cos(u * Math.PI * rate * 0.6 + time * 1.4 + s) * swing * 0.7;
      }
      line.geometry.attributes.position.needsUpdate = true;
    }
  }

  /** Per-node brightness: depth, a twinkle, and a lift with the audio. */
  _shade(elapsed) {
    const colour = this._nodeColor;
    const gain = 0.5 + this._intensity * 0.3 + this._level * 0.3;
    const twinkle = this._reduced ? 0 : 0.16;

    for (let i = 0; i < this._count; i += 1) {
      const z = this._base[i * 3 + 2];
      let brightness = 0.36 + Math.max(0, z) * 0.55 + this._accent[i] * 0.85;
      brightness *= 1 + Math.sin(elapsed * 1.6 + this._phase[i]) * twinkle;

      const value = brightness * gain;
      this._colors[i * 3] = colour.r * value;
      this._colors[i * 3 + 1] = colour.g * value;
      this._colors[i * 3 + 2] = colour.b * value;
    }
    this._nodes.geometry.attributes.color.needsUpdate = true;
  }

  _observeSize() {
    this._observer = new ResizeObserver(() => this._onResize());
    this._observer.observe(this._canvas.parentElement);
    this._onResize();
  }

  _onResize() {
    const parent = this._canvas.parentElement;
    const width = parent.clientWidth;
    const height = parent.clientHeight;
    if (width < 2 || height < 2) return;
    this._renderer.setSize(width, height, false);
    this._camera.aspect = width / height;
    this._camera.updateProjectionMatrix();
  }

  _showFallback() {
    const note = document.createElement('div');
    note.className = 'no-webgl';
    note.textContent =
      'This browser cannot draw the assistant (WebGL is unavailable). '
      + 'Voice still works — the transcript is below.';
    this._canvas.parentElement.appendChild(note);
  }
}
