/**
 * Two rendering helpers every avatar theme needs.
 *
 * Separate from `head.js` because neither has anything to do with anatomy: one makes the
 * dot, the other moves the lines.
 */

import * as THREE from '../vendor/three.module.min.js';

/** Copy a list of index pairs into a flat LineSegments position buffer. */
export function writeSegments(pairs, positions, out) {
  for (let i = 0; i < pairs.length; i += 2) {
    const a = pairs[i] * 3;
    const b = pairs[i + 1] * 3;
    const at = (i / 2) * 6;
    out[at] = positions[a];
    out[at + 1] = positions[a + 1];
    out[at + 2] = positions[a + 2];
    out[at + 3] = positions[b];
    out[at + 4] = positions[b + 1];
    out[at + 5] = positions[b + 2];
  }
}

/**
 * A soft round dot, drawn once into a canvas and reused for every node.
 *
 * The default point is an opaque square. This single texture is the difference between
 * "scatter plot" and "hologram", and it costs one 64px canvas at start-up.
 */
export function makeGlowSprite() {
  const size = 64;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const context = canvas.getContext('2d');
  const gradient = context.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  // A hot core with a long falloff: the falloff is what overlaps into a glow when
  // neighbouring nodes are drawn additively.
  gradient.addColorStop(0, 'rgba(255,255,255,1)');
  gradient.addColorStop(0.25, 'rgba(255,255,255,0.85)');
  gradient.addColorStop(0.5, 'rgba(255,255,255,0.28)');
  gradient.addColorStop(1, 'rgba(255,255,255,0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.needsUpdate = true;
  return texture;
}
