/**
 * The holographic avatar: a face built from nodes and neural-network lines (M29 §12-16).
 *
 * Three.js, vendored — an air-gapped host has no CDN, so the library ships with the
 * bundle exactly as Bootstrap and Chart.js do (Rule 4).
 *
 * **The face is generated, never loaded.** No mesh file, no texture: a head is sampled
 * parametrically at start-up and the mesh follows the sampling grid. An asset would be
 * one more thing to ship, and a face defined in code is one whose mouth we can actually
 * drive from audio.
 *
 * The construction, in the order it reads on screen:
 *
 *     outline -> relief -> nodes, minus the eye and mouth voids -> grid mesh
 *             -> coarse facets -> brows, lids, nose, lips, neck -> glow -> rings
 *
 * **Four things make it a face rather than a decorated ball**, and they are worth naming
 * because each one replaced an attempt that measured correctly and still looked wrong:
 *
 * 1. *The eyes and mouth are holes* (:func:`inEyeVoid`) — cut out of the dot field, not
 *    drawn into it. An earlier version made them the brightest nodes on the head, and it
 *    read as a mask; a bright eye is a decoration on a surface, a dark one is a hole
 *    through it.
 * 2. *The outline is stated* (:data:`HEAD_OUTLINE`) rather than left to an ellipsoid.
 *    From the front the silhouette **is** the face, and relief cannot fix it: relief is
 *    strongest at the centre and fades to nothing exactly at the edge.
 * 3. *The proportions are the artist's canon*, not taste — a head is 1.4 times as tall as
 *    it is wide, and the eyes sit halfway down it.
 * 4. *The mesh follows the sampling grid*, not proximity. See :meth:`_buildLinks`.
 *
 * Only the front of the head is populated: drawn additively, a closed head shows its back
 * through its face. :data:`MAX_TURN` is what keeps the open edge out of view.
 *
 * **The glow is the whole look.** Each node is drawn as a soft radial sprite rather than
 * the default square, and brightness is written per node every frame — depth shading so
 * the head has volume, a sweep so light travels across it, and a lift with the audio.
 * Real bloom would need a post-processing pass, which is another vendored file and
 * another full-screen pass per frame; a good sprite gets most of the way for neither.
 *
 * **Speaking and listening are driven by amplitude, not by a timer.** The jaw drops with
 * the audio being played, a waveform ring traces what is being said, and pulse rings
 * travel outward while the microphone is open (§16).
 *
 * **Sizing is observed, not assumed.** The avatar is constructed while the page is still
 * hidden behind the sign-in gate, so the canvas measures 0×0 at that moment. A
 * ResizeObserver picks up the size the instant the stage is revealed — a `window.resize`
 * listener never fires for that, and the result is a canvas that stays blank for ever.
 *
 * **Reduced motion is honoured** (§37): the face holds still, and state is carried by
 * colour.
 */

import * as THREE from '../vendor/three.module.min.js';
import {
  BROW_Y, CHIN_Y, CROWN_Y, EYE_HALF, EYE_X, EYE_Y, MAX_TURN, MOUTH_HALF, MOUTH_Y,
  NOSE_BASE_Y, bump, headHalfDepth, headHalfWidth, sculptFace, surfaceZ,
} from './head.js';
import { makeGlowSprite, writeSegments } from './glow.js';

/** State → palette. Colour is what still distinguishes states under reduced motion. */
const PALETTE = {
  idle:      { node: 0x4f8dff, line: 0x1b3a72, glow: 0x2f6fed, intensity: 0.5 },
  listening: { node: 0x38e8ff, line: 0x0e7490, glow: 0x22d3ee, intensity: 1.0 },
  thinking:  { node: 0xb79bff, line: 0x5b21b6, glow: 0x8b5cf6, intensity: 0.85 },
  tool:      { node: 0xffc94d, line: 0xb45309, glow: 0xf59e0b, intensity: 0.95 },
  speaking:  { node: 0x7df3ff, line: 0x0369a1, glow: 0x38bdf8, intensity: 1.0 },
  error:     { node: 0xff8f8f, line: 0x991b1b, glow: 0xef4444, intensity: 0.7 },
};

/** Regions, so the mouth can move while the skull does not. */
const REGION = { FACE: 0, LID: 1, LIP_UPPER: 2, LIP_LOWER: 3, NOSE: 4, BROW: 5, NECK: 6 };

const ROWS = 46;           // horizontal slices from crown to chin
const COLUMNS = 30;        // samples across the widest slice
const LINK_RADIUS = 0.30;  // nodes closer than this are wired together
const MAX_LINKS = 7000;    // links, not array entries — see _buildLinks
const HUB_SPACING = 0.26;  // how far apart the coarse polygon vertices sit
const PULSE_RINGS = 3;     // concentric pulses that travel outward while listening

export class HologramAvatar {
  constructor(canvas) {
    this._canvas = canvas;
    this._state = 'idle';
    this._level = 0;
    this._targetLevel = 0;
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
      // No WebGL — a locked-down browser or a VM with no GPU. Say so, rather than
      // leaving a blank rectangle that looks like the page failed to load.
      this._failed = true;
      this._showFallback();
      return;
    }
    // Capped at 2: a 3x display gains nothing visible and costs nine times the
    // fragments, which is the difference between 60 FPS and 20 on an integrated GPU.
    this._renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    this._group = new THREE.Group();
    this._scene.add(this._group);

    this._sprite = makeGlowSprite();
    this._buildFace();
    this._buildLinks();
    this._buildPolygons();
    this._buildAmbient();
    this._buildWaveform();
    this._buildPulses();
    this._buildRing();
    this._applyPalette(PALETTE.idle);

    this._observeSize();
    this._renderer.setAnimationLoop(() => this._frame());
  }

  // -- construction --------------------------------------------------------

  /**
   * Sample the head, cut the eyes and mouth out of it, and mark what should glow.
   *
   * Density is weighted towards the middle of the face, because that is where a viewer
   * looks. Even sampling spends as many nodes on the top of the skull — which carries no
   * information — as on the eyes and mouth, which carry all of it.
   *
   * Each node carries an **accent**: how much brighter than the plain surface it should
   * be. It is computed here, once, rather than per frame — the rim of the silhouette, the
   * ridge of the nose and the gutters either side of it, the lids and the lips — which
   * leaves :meth:`_shade` a single multiply per node.
   */
  _buildFace() {
    const base = [];
    const regions = [];
    const accent = [];
    /** One place that appends a node, so region and accent can never fall out of step. */
    const put = (x, y, z, region, glow = 0) => {
      base.push(x, y, z);
      regions.push(region);
      accent.push(glow);
      return regions.length - 1;
    };
    // The sampling grid, remembered so the links can follow it — see :meth:`_buildLinks`.
    const rows = [];

    // Rows down the head. Concentrated between the brow and the chin, because that band
    // carries every feature a viewer reads and the cranium carries none.
    for (let row = 0; row < ROWS; row += 1) {
      const t = row / (ROWS - 1);
      const y = CROWN_Y - (CROWN_Y - CHIN_Y) * (t - Math.sin(t * Math.PI * 2) * 0.06);

      const halfWidth = headHalfWidth(y);
      const halfDepth = headHalfDepth(y);
      if (halfWidth < 0.02) continue;

      // Arc across the front of this cross-section. Only the front is populated: a full
      // sphere of dots drawn additively shows the back *through* the face and reads as a
      // ball of noise. The arc runs to ±112°, and the open edge stays hidden behind the
      // silhouette for any turn under 22° — which is the bound :meth:`_frame` respects.
      const columns = Math.max(9, Math.round(COLUMNS * (0.45 + halfWidth * 0.75)));
      const uMax = MAX_TURN + Math.PI / 2;
      const line = [];

      for (let column = 0; column <= columns; column += 1) {
        const u = -uMax + (column / columns) * uMax * 2;
        const x = halfWidth * Math.sin(u);
        if (inEyeVoid(x, y) || inMouthVoid(x, y)) continue;

        // An elliptical cross-section, deepest at the centre line and meeting zero at the
        // silhouette, so the outline is exactly headHalfWidth and nothing else.
        const across = Math.cos(u);
        const z = halfDepth * across + sculptFace(x, y) * Math.max(0, across);

        // Two highlights, both taken from the reference: the rim of the head, and a
        // stripe down the nose. They are what stop a uniform field of dots reading flat.
        const rim = Math.abs(Math.sin(u)) ** 6 * 0.55;
        // The nose reads from the contrast either side of it, not from its own outline.
        // A bright ridge alone is a stripe; a bright ridge between two dim gutters is a
        // nose, and it costs one extra term.
        const ridge = 0.62 * bump(x, y - (EYE_Y - 0.22), 0.07, 0.32);
        const gutter = -0.22 * bump(Math.abs(x) - 0.23, y - (EYE_Y - 0.26), 0.105, 0.30);
        const glow = rim + ridge + gutter;
        line.push(put(x, y, z, REGION.FACE, glow));
      }
      if (line.length > 1) rows.push(line);
    }

    this._addNeck(put);
    this._rows = rows;
    // Everything after this point is a feature cluster with no place in the grid, so the
    // link builder wires it by proximity instead.
    this._featureStart = regions.length;

    this._addSilhouette(put);
    this._addBrow(put, -1);
    this._addBrow(put, 1);
    this._addEyelid(put, -EYE_X);
    this._addEyelid(put, EYE_X);
    this._addNose(put);
    this._addMouth(put);

    this._base = new Float32Array(base);
    this._regions = Uint8Array.from(regions);
    this._accent = Float32Array.from(accent);
    this._count = this._regions.length;
    // A phase per node, so the shimmer never looks like one synchronised blink.
    this._phase = new Float32Array(this._count);
    for (let i = 0; i < this._count; i += 1) this._phase[i] = Math.random() * Math.PI * 2;

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(this._base), 3));
    this._colors = new Float32Array(this._count * 3);
    geometry.setAttribute('color', new THREE.BufferAttribute(this._colors, 3));

    this._nodeMaterial = new THREE.PointsMaterial({
      size: 0.062,
      map: this._sprite,
      // Per-node brightness is written every frame — depth, sweep and audio in one pass.
      vertexColors: true,
      transparent: true,
      // Additive, so overlapping nodes brighten instead of occluding — what makes a
      // dense region look like light rather than like dots.
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      sizeAttenuation: true,
    });
    this._nodes = new THREE.Points(geometry, this._nodeMaterial);
    this._group.add(this._nodes);
  }

  _addSilhouette(put) {
    // The outline as its own dense, bright chain. In the reference this edge is the
    // brightest thing in the picture, and it is what the eye traces first: a face is
    // recognised from its outline before a single feature inside it is read. The row
    // sampling above already reaches the edge, but it arrives there at whatever spacing
    // each row happens to have, which leaves the outline ragged.
    for (const side of [-1, 1]) {
      for (let i = 0; i <= 54; i += 1) {
        const y = CROWN_Y - (CROWN_Y - CHIN_Y) * (i / 54);
        const x = side * headHalfWidth(y);
        put(x, y, surfaceZ(x, y), REGION.FACE, 0.75);
      }
    }
  }

  _addBrow(put, side) {
    // The single most recognisable feature after the eyes, and the one most often left
    // out: without brows a face reads as a mannequin. Angled slightly down towards the
    // nose, which is what stops the expression looking surprised.
    for (let i = 0; i < 13; i += 1) {
      const t = i / 12;
      const x = side * (0.20 + t * 0.60);
      const y = BROW_Y + Math.sin(t * Math.PI) * 0.055 - t * 0.045;
      put(x, y, surfaceZ(x, y) + 0.015, REGION.BROW, 0.55);
    }
  }

  _addEyelid(put, centreX) {
    // The socket itself is empty — see :func:`inEyeVoid`. What is drawn is the lid around
    // it: an almond, because eyes are lens-shaped and a circle reads as a button. The
    // bright ring against the dark hole is the whole effect.
    for (let i = 0; i < 26; i += 1) {
      const angle = (i / 26) * Math.PI * 2;
      const lid = Math.sin(angle);
      // The upper lid arcs higher than the lower one — an almond, not an ellipse.
      const x = centreX + Math.cos(angle) * EYE_HALF;
      const y = EYE_Y + lid * (lid > 0 ? 0.150 : 0.115);
      put(x, y, surfaceZ(x, y) + 0.012, REGION.LID, 0.9);
    }
  }

  _addNose(put) {
    // A ridge from between the brows down to a tip, then the nostril wings. The wings
    // matter: a bare ridge reads as a crease, and the two points at its base are what
    // turn it into a nose.
    for (let i = 0; i < 11; i += 1) {
      const t = i / 10;
      const y = BROW_Y - t * (BROW_Y - NOSE_BASE_Y);
      // Rises all the way to the tip at t = 0.85, then falls back sharply to the base. A
      // curve that peaks earlier puts the highest point between the eyes, which reads as
      // a brow lump rather than a nose.
      const protrusion = t < 0.85
        ? Math.sin((t / 0.85) * Math.PI * 0.5)
        : 1 - ((t - 0.85) / 0.15) * 0.4;
      put(0, y, surfaceZ(0, y) + protrusion * 0.06, REGION.NOSE, 0.5 + protrusion * 0.4);
    }
    for (const side of [-1, 1]) {
      for (let i = 0; i < 5; i += 1) {
        const angle = (i / 4) * Math.PI * 0.9 - 0.22;
        // The wings line up with the inner corners of the eyes, which is the proportion
        // that makes a nose look the right width for the face it is on.
        const x = side * (0.085 + Math.sin(angle) * 0.145);
        const y = NOSE_BASE_Y + 0.035 + Math.cos(angle) * 0.065;
        put(x, y, surfaceZ(x, y) + 0.02, REGION.NOSE, 0.85);
      }
    }
  }

  _addMouth(put) {
    // The gap between the lips is cut away — see :func:`inMouthVoid`. These are the lip
    // edges around it. The upper lip carries a cupid's bow and the lower one is fuller;
    // the asymmetry is most of what says "mouth", and it is also what lets the lower lip
    // drop on its own when the assistant speaks.
    for (let i = 0; i < 28; i += 1) {
      const t = i / 27;
      const x = (t - 0.5) * 2 * MOUTH_HALF;
      const across = Math.cos((t - 0.5) * Math.PI);          // 0 at the corners, 1 centre
      // Two peaks either side of the centre, dipping between them: a cupid's bow.
      const bow = Math.cos((t - 0.5) * Math.PI * 4) * 0.013;
      const y = MOUTH_Y + 0.095 + across * 0.02 + bow;
      put(x, y, surfaceZ(x, y) + 0.02, REGION.LIP_UPPER, 0.7);
    }

    for (let i = 0; i < 28; i += 1) {
      const t = i / 27;
      const x = (t - 0.5) * 2 * MOUTH_HALF;
      const across = Math.cos((t - 0.5) * Math.PI);
      const y = MOUTH_Y - 0.095 - across * 0.045;
      put(x, y, surfaceZ(x, y) + 0.02, REGION.LIP_LOWER, 0.7);
    }
  }

  _addNeck(put) {
    // A neck, running off the bottom of the frame. Without one the head is an object
    // floating in space; with one it is a person, and the jaw gains something to overhang
    // — which is what gives the chin an edge instead of ending in nothing.
    //
    // Set back behind the chin and shaded down in :meth:`_shade`, so it stays scenery.
    for (let row = 0; row <= 10; row += 1) {
      const t = row / 10;
      const y = -0.95 - t * 1.25;
      const halfWidth = 0.42 + t * 0.30;
      for (let column = 0; column <= 18; column += 1) {
        const u = -Math.PI * 0.5 + (column / 18) * Math.PI;
        // Dimmed towards the bottom as well as overall, so it fades into the frame edge
        // instead of ending in a hard bright line across the picture.
        put(halfWidth * Math.sin(u), y, 0.46 * Math.cos(u) - 0.18, REGION.NECK, -t * 0.22);
      }
    }
  }

  /**
   * Wire the nodes together: the grid along its own topology, the features by proximity.
   *
   * **Proximity alone produces stripes.** Within a row a node's three nearest neighbours
   * are all beside it, so a nearest-N rule spends the entire budget going sideways and
   * draws a set of horizontal bands with nothing joining them — which is exactly what the
   * first render of this looked like. The sampling already knows which nodes are in which
   * row and column, so the grid is built from that directly and comes out as a mesh.
   *
   * Rows are broken by the eye and mouth voids, so both directions check the gap before
   * joining: without that, every row crossing a socket draws a line straight over it and
   * fills the hole back in.
   */
  _buildLinks() {
    const pairs = [];

    for (let r = 0; r < this._rows.length; r += 1) {
      const row = this._rows[r];
      for (let c = 0; c + 1 < row.length; c += 1) {
        if (this._gap(row[c], row[c + 1]) < LINK_RADIUS) pairs.push(row[c], row[c + 1]);
      }
      const below = this._rows[r + 1];
      if (!below) continue;
      // Down to whichever node in the next row sits nearest in x — the column direction.
      for (const index of row) {
        const x = this._base[index * 3];
        let best = -1;
        let bestDistance = Infinity;
        for (const candidate of below) {
          const distance = Math.abs(this._base[candidate * 3] - x);
          if (distance < bestDistance) { bestDistance = distance; best = candidate; }
        }
        if (best >= 0 && this._gap(index, best) < LINK_RADIUS) pairs.push(index, best);
      }
    }

    // Features: brows, lids, nose, lips, silhouette. These sit on top of the grid rather
    // than in it, so each takes its nearest few neighbours wherever they are.
    const radiusSquared = LINK_RADIUS * LINK_RADIUS;
    for (let a = this._featureStart; a < this._count && pairs.length / 2 < MAX_LINKS; a += 1) {
      let linked = 0;
      for (let b = 0; b < this._count; b += 1) {
        if (b === a) continue;
        const dx = this._base[a * 3] - this._base[b * 3];
        const dy = this._base[a * 3 + 1] - this._base[b * 3 + 1];
        const dz = this._base[a * 3 + 2] - this._base[b * 3 + 2];
        if (dx * dx + dy * dy + dz * dz > radiusSquared) continue;
        pairs.push(a, b);
        linked += 1;
        if (linked >= 3) break;
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
    this._writeLinks(new Float32Array(this._base));
  }

  /** Distance between two nodes. Used to refuse a link that would cross a void. */
  _gap(a, b) {
    const dx = this._base[a * 3] - this._base[b * 3];
    const dy = this._base[a * 3 + 1] - this._base[b * 3 + 1];
    const dz = this._base[a * 3 + 2] - this._base[b * 3 + 2];
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  }

  /**
   * A second, coarser network of long bright lines over the front of the face.
   *
   * The proximity links above join every node to its neighbours, which produces a fine
   * even grid — accurate, and visually inert. The reference gets its character from a
   * *sparse* set of long lines cutting across the forehead and cheeks in big triangles,
   * on top of that grid. Two scales of structure is the whole trick; one is wallpaper.
   *
   * Vertices are picked by thinning: walk the nodes and keep one only if it is far enough
   * from every vertex kept so far. That gives an even scatter without a Delaunay
   * triangulation, which would be several hundred lines of code for a result no one would
   * be able to tell apart at this density.
   */
  _buildPolygons() {
    const hubs = [];
    const gapSquared = HUB_SPACING * HUB_SPACING;
    for (let i = 0; i < this._count; i += 1) {
      // The front of the head only. A long line joining two nodes on opposite sides
      // passes straight through the skull and reads as a wire, not as a facet.
      if (this._regions[i] === REGION.NECK || this._base[i * 3 + 2] < 0.35) continue;
      const x = this._base[i * 3];
      const y = this._base[i * 3 + 1];
      const z = this._base[i * 3 + 2];
      let clear = true;
      for (const hub of hubs) {
        const dx = x - this._base[hub * 3];
        const dy = y - this._base[hub * 3 + 1];
        const dz = z - this._base[hub * 3 + 2];
        if (dx * dx + dy * dy + dz * dz < gapSquared) { clear = false; break; }
      }
      if (clear) hubs.push(i);
    }

    // Each vertex to its three nearest, deduplicated so a shared edge is not drawn twice
    // at double brightness.
    const seen = new Set();
    const pairs = [];
    for (const a of hubs) {
      const ranked = hubs
        .filter((b) => b !== a)
        .map((b) => {
          const dx = this._base[a * 3] - this._base[b * 3];
          const dy = this._base[a * 3 + 1] - this._base[b * 3 + 1];
          const dz = this._base[a * 3 + 2] - this._base[b * 3 + 2];
          return { b, distance: dx * dx + dy * dy + dz * dz };
        })
        .sort((one, two) => one.distance - two.distance)
        .slice(0, 3);
      for (const { b } of ranked) {
        const key = a < b ? `${a}:${b}` : `${b}:${a}`;
        if (seen.has(key)) continue;
        seen.add(key);
        pairs.push(a, b);
      }
    }

    this._polyPairs = Uint16Array.from(pairs);
    this._polyPositions = new Float32Array((this._polyPairs.length / 2) * 6);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(this._polyPositions, 3));
    this._polyMaterial = new THREE.LineBasicMaterial({
      transparent: true,
      opacity: 0.5,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._polygons = new THREE.LineSegments(geometry, this._polyMaterial);
    this._group.add(this._polygons);
  }

  /** A sparse field behind the head, so the face sits in space rather than on a flat void. */
  _buildAmbient() {
    const count = 520;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const radius = 2.5 + Math.random() * 1.9;
      positions[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = radius * Math.cos(phi) * 0.8;
      positions[i * 3 + 2] = radius * Math.sin(phi) * Math.sin(theta) - 0.8;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    this._ambientMaterial = new THREE.PointsMaterial({
      size: 0.05,
      map: this._sprite,
      transparent: true,
      opacity: 0.4,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._ambient = new THREE.Points(geometry, this._ambientMaterial);
    this._scene.add(this._ambient);
  }

  /**
   * A ring that traces the audio, below the chin.
   *
   * This is the clearest read of "it is speaking to me": a circle whose radius wobbles
   * with amplitude, so the visual is the sound rather than an animation beside it.
   */
  _buildWaveform() {
    this._waveSegments = 128;
    this._wavePositions = new Float32Array((this._waveSegments + 1) * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(this._wavePositions, 3));
    this._waveMaterial = new THREE.LineBasicMaterial({
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._waveform = new THREE.Line(geometry, this._waveMaterial);
    this._waveform.position.y = -1.80;
    this._waveform.rotation.x = Math.PI / 2.3;
    this._scene.add(this._waveform);
  }

  /** Pulses travelling outward while the microphone is open. */
  _buildPulses() {
    this._pulses = [];
    for (let i = 0; i < PULSE_RINGS; i += 1) {
      const material = new THREE.MeshBasicMaterial({
        transparent: true,
        opacity: 0,
        side: THREE.DoubleSide,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(new THREE.RingGeometry(1.9, 1.94, 96), material);
      mesh.rotation.x = Math.PI / 2.3;
      mesh.position.y = -1.74;
      this._scene.add(mesh);
      this._pulses.push({ mesh, material, phase: i / PULSE_RINGS });
    }
  }

  _buildRing() {
    this._ringMaterial = new THREE.MeshBasicMaterial({
      transparent: true,
      opacity: 0.4,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this._ring = new THREE.Mesh(new THREE.RingGeometry(1.95, 2.02, 128), this._ringMaterial);
    // Tilted and set below the chin, so it reads as a plinth the head sits above rather
    // than a halo around it.
    this._ring.rotation.x = Math.PI / 2.3;
    this._ring.position.y = -1.74;
    this._scene.add(this._ring);
  }

  // -- public API (§15) ----------------------------------------------------
  setState(state) {
    if (!PALETTE[state] || state === this._state) return;
    this._state = state;
    if (!this._failed) this._applyPalette(PALETTE[state]);
  }

  get state() {
    return this._state;
  }

  /** Audio amplitude, 0..1. Smoothed towards, never snapped to. */
  setAudioLevel(level) {
    this._targetLevel = Math.max(0, Math.min(1, level || 0));
  }

  /** Re-measure. Called by the ResizeObserver, and safe to call by hand. */
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
    this._linkMaterial.color.setHex(palette.line);
    this._polyMaterial.color.setHex(palette.node);
    this._ambientMaterial.color.setHex(palette.node);
    this._ringMaterial.color.setHex(palette.glow);
    this._waveMaterial.color.setHex(palette.node);
    for (const pulse of this._pulses) pulse.material.color.setHex(palette.glow);
    this._intensity = palette.intensity;
  }

  _frame() {
    const elapsed = this._clock.getElapsedTime();
    // Smoothing, not assignment: raw amplitude is jittery at 60 Hz and makes the jaw
    // chatter. A ~100 ms time constant tracks speech without twitching.
    this._level += (this._targetLevel - this._level) * 0.18;

    const positions = this._nodes.geometry.attributes.position.array;
    const talking = this._state === 'speaking';
    const listening = this._state === 'listening';
    const talk = talking ? this._level : 0;

    if (this._reduced) {
      // Held still — but the mouth still opens, because that is information rather than
      // decoration: it is how you can tell it is talking with the sound off.
      positions.set(this._base);
      this._openMouth(positions, talk * 0.6);
      this._shade(elapsed, false);
      this._nodes.geometry.attributes.position.needsUpdate = true;
      this._writeLinks(positions);
      this._renderer.render(this._scene, this._camera);
      return;
    }

    // Every node breathes on its own phase, so the network shimmers rather than pulsing
    // as one object.
    const shimmer = 0.012 + this._level * 0.022;
    for (let i = 0; i < this._count; i += 1) {
      const scale = 1 + Math.sin(elapsed * 1.7 + this._phase[i]) * shimmer;
      positions[i * 3] = this._base[i * 3] * scale;
      positions[i * 3 + 1] = this._base[i * 3 + 1] * scale;
      positions[i * 3 + 2] = this._base[i * 3 + 2] * scale;
    }

    this._openMouth(positions, talk);
    this._blink(positions, elapsed);
    this._shade(elapsed, true);

    this._nodes.geometry.attributes.position.needsUpdate = true;
    this._writeLinks(positions);
    this._updateWaveform(elapsed, talking || listening);
    this._updatePulses(elapsed, listening);

    // A slight turn of the head. Small deliberately: a face that swings around is a
    // character, and this is an instrument. Thinking turns further and slower — it reads
    // as considering something — but never past MAX_TURN, because beyond that the open
    // back of the shell comes into view. An earlier version spun continuously here, which
    // rotated the hollow side to the front every few seconds.
    const thinking = this._state === 'thinking';
    const turn = Math.sin(elapsed * (thinking ? 0.5 : 0.35)) * (thinking ? 0.30 : 0.11);
    this._group.rotation.y = turn;
    this._group.rotation.x = Math.sin(elapsed * 0.5) * 0.028;
    this._group.scale.setScalar(1 + this._level * 0.03);

    this._linkMaterial.opacity = 0.1 + this._intensity * 0.14 + this._level * 0.24;
    this._polyMaterial.opacity = 0.16 + this._intensity * 0.2 + this._level * 0.3;
    this._ambientMaterial.opacity = 0.22 + this._level * 0.28;
    this._ambient.rotation.y -= this._state === 'tool' ? 0.0035 : 0.0009;
    this._ring.scale.setScalar(1 + this._level * 0.14);
    this._ringMaterial.opacity = 0.18 + this._intensity * 0.28;

    this._renderer.render(this._scene, this._camera);
  }

  /**
   * Write per-node brightness: depth, a travelling sweep, and the audio.
   *
   * One pass over the colour buffer does what three separate effects would otherwise
   * need, and it is what gives the head volume — without it every dot is equally bright
   * and the face reads flat however well it is shaped.
   */
  _shade(elapsed, animated) {
    const colour = this._nodeColor;
    const gain = 0.55 + this._intensity * 0.3 + this._level * 0.35;
    // A band of light travelling down the head, wrapping every few seconds. The range
    // covers the neck as well, or the sweep would stop dead at the chin.
    const sweepY = animated ? ((elapsed * 0.55) % 4.2) - 2.2 : 99;

    for (let i = 0; i < this._count; i += 1) {
      const z = this._base[i * 3 + 2];
      // Front-facing nodes brighter: this is the depth cue that makes it a head. The
      // accent is baked in at build time — the rim of the silhouette, the ridge of the
      // nose, the lids and the lips — so this loop stays one multiply per node.
      let brightness = 0.34 + Math.max(0, z) * 0.55 + this._accent[i] * 0.8;
      // The neck is scenery. Bright, it competes with the face for attention; dim, it
      // does the one job it has, which is to stop the head floating.
      if (this._regions[i] === REGION.NECK) brightness *= 0.42;

      if (animated) {
        const distance = Math.abs(this._base[i * 3 + 1] - sweepY);
        if (distance < 0.32) brightness += (1 - distance / 0.32) * 0.7;
      }

      const value = brightness * gain;
      this._colors[i * 3] = colour.r * value;
      this._colors[i * 3 + 1] = colour.g * value;
      this._colors[i * 3 + 2] = colour.b * value;
    }
    this._nodes.geometry.attributes.color.needsUpdate = true;
  }

  /**
   * Drop the jaw with the audio.
   *
   * The mandible is found from the geometry, not from a region tag: everything below
   * :func:`jawBoundary` swings about an axis through the ears, so how far a node travels
   * depends on how far in front of that axis it sits. The chin, which is furthest
   * forward, moves most; the skin by the ear barely moves at all.
   *
   * The earlier version tagged every node below a fixed height as "jaw" and translated
   * the lot by one amount. Two things went wrong with that, and both are visible: a step
   * at the boundary opened a crack straight across the cheeks, and the lower lip — which
   * had a drop of its own — tore away from the skin immediately below it, leaving the
   * links between them stretched out like threads.
   */
  _openMouth(positions, amount) {
    if (amount <= 0.001) return;
    const swing = amount * 0.155;
    for (let i = 0; i < this._count; i += 1) {
      if (this._regions[i] === REGION.NECK) continue;
      const y = this._base[i * 3 + 1];
      const boundary = jawBoundary(this._base[i * 3]);
      if (y >= boundary) continue;

      // Eased in over the top half of the jaw. The band has to be wider than the longest
      // link that crosses it (LINK_RADIUS), or a single link with one end above the hinge
      // and one below takes the whole drop as stretch and shows as a torn thread.
      const ease = Math.min(1, (boundary - y) / 0.40);
      // How far in front of the hinge axis, which runs through the ears and behind them.
      const reach = this._base[i * 3 + 2] + 0.35;
      let drop = swing * reach * ease;
      // The lower lip travels a little further than the skin under it. That difference is
      // what parts the lips — without it the whole chin simply lowers, mouth still shut.
      if (this._regions[i] === REGION.LIP_LOWER) drop += swing * 0.35;

      positions[i * 3 + 1] -= drop;
      positions[i * 3 + 2] += drop * 0.12;
    }
  }

  /** An occasional blink. Rare and quick — the eyes are otherwise a fixed stare. */
  _blink(positions, elapsed) {
    const cycle = elapsed % 6.5;
    if (cycle > 0.16) return;
    const closed = 1 - Math.abs(cycle - 0.08) / 0.08;
    for (let i = 0; i < this._count; i += 1) {
      if (this._regions[i] !== REGION.LID) continue;
      positions[i * 3 + 1] += (EYE_Y - this._base[i * 3 + 1]) * closed;
    }
  }

  /** A circle whose radius follows the audio — the sound made visible. */
  _updateWaveform(elapsed, active) {
    const target = active ? 0.25 + this._level * 0.75 : 0;
    this._waveMaterial.opacity += (target - this._waveMaterial.opacity) * 0.12;
    if (this._waveMaterial.opacity < 0.01) return;

    for (let i = 0; i <= this._waveSegments; i += 1) {
      const angle = (i / this._waveSegments) * Math.PI * 2;
      // Three harmonics, so it reads as a waveform rather than as a wobbling circle.
      const wobble =
        Math.sin(angle * 7 + elapsed * 5) * 0.06 +
        Math.sin(angle * 13 - elapsed * 3) * 0.035 +
        Math.sin(angle * 3 + elapsed * 2) * 0.05;
      const radius = 1.68 + wobble * (0.25 + this._level * 2.6);
      this._wavePositions[i * 3] = Math.cos(angle) * radius;
      this._wavePositions[i * 3 + 1] = Math.sin(angle) * radius;
      this._wavePositions[i * 3 + 2] = 0;
    }
    this._waveform.geometry.attributes.position.needsUpdate = true;
  }

  /** Rings travelling outward while the microphone is open. */
  _updatePulses(elapsed, listening) {
    for (const pulse of this._pulses) {
      if (!listening) {
        pulse.material.opacity += (0 - pulse.material.opacity) * 0.15;
        continue;
      }
      // Each ring runs the same 2.4 s journey, offset so they never leave together.
      const t = ((elapsed / 2.4) + pulse.phase) % 1;
      pulse.mesh.scale.setScalar(1 + t * 0.85);
      pulse.material.opacity = (1 - t) * (0.18 + this._level * 0.5);
    }
  }

  /** Rewrite every line from wherever its two nodes currently are. */
  _writeLinks(positions) {
    writeSegments(this._pairs, positions, this._linkPositions);
    this._links.geometry.attributes.position.needsUpdate = true;
    if (!this._polygons) return;
    writeSegments(this._polyPairs, positions, this._polyPositions);
    this._polygons.geometry.attributes.position.needsUpdate = true;
  }

  /**
   * Watch the container, do not wait for a window resize.
   *
   * The avatar is built while the stage is still hidden behind the sign-in gate, so at
   * construction the canvas is 0×0. `window.resize` never fires when a parent stops
   * being `display: none`, so a listener on it leaves the canvas blank for the whole
   * session — which is exactly the bug this replaces.
   */
  _observeSize() {
    this._observer = new ResizeObserver(() => this._onResize());
    this._observer.observe(this._canvas.parentElement);
    this._onResize();
  }

  _onResize() {
    const parent = this._canvas.parentElement;
    const width = parent.clientWidth;
    const height = parent.clientHeight;
    // Nothing to draw into yet, and dividing by it would make the camera's aspect NaN.
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

/**
 * The top edge of the lower jaw, as a height at a given x.
 *
 * It runs from just under the lower lip at the centre line up to the ear at the side —
 * which is what the mandible actually does, and why a horizontal cut-off tears the face.
 */
function jawBoundary(x) {
  return -0.80 + Math.min(1, Math.abs(x) / 0.85) * 0.26;
}

/**
 * Is this point inside an eye socket, or between the lips?
 *
 * **These are holes, and the holes are the face.** Nothing else in this file contributes
 * as much: two dark almonds and a dark slit in a field of glowing dots read as a face
 * instantly, at any size, on any silhouette. An earlier version drew the eyes as the
 * brightest nodes on the head and it still read as a mask, because a bright eye is a
 * decoration on a surface whereas a dark one is a hole through it.
 */
function inEyeVoid(x, y) {
  const dx = (Math.abs(x) - EYE_X) / EYE_HALF;
  const lift = y - EYE_Y;
  const dy = lift / (lift > 0 ? 0.150 : 0.115);
  return dx * dx + dy * dy < 1;
}

function inMouthVoid(x, y) {
  const dx = x / MOUTH_HALF;
  const dy = (y - MOUTH_Y) / 0.09;
  return dx * dx + dy * dy < 1;
}
