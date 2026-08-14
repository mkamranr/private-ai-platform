/**
 * The head itself: proportions, outline and relief, shared by every avatar theme.
 *
 * This lives apart from any one theme because it is the expensive part to get right and
 * the easy part to get wrong. The outline below is a twenty-one row anatomical table, and
 * a second copy of it in a second theme would drift from the first time either was tuned
 * — leaving two avatars that are recognisably different people.
 *
 * Coordinates: the crown is at y = +1.46 and the chin at y = -1.52, x is half-width, and
 * +z is towards the viewer. Themes import what they need and sample it their own way.
 */

/**
 * Landmarks, from the artist's canon rather than from guesswork.
 *
 * Every feature is placed from these, so the face moves as one when a proportion is
 * corrected. The one that matters most is `EYE_Y`: the eyes sit exactly **halfway between
 * the crown and the chin**. Almost everyone places them too high — including an earlier
 * version of this file, which put them a tenth of a head above the midpoint and read as a
 * child's face with an oversized jaw.
 */
export const CROWN_Y = 1.46;
export const CHIN_Y = -1.52;
export const EYE_Y = -0.03;        // the crown-to-chin midpoint
export const BROW_Y = 0.16;
export const NOSE_BASE_Y = -0.55;
export const MOUTH_Y = -0.86;
export const EYE_X = 0.52;         // half the distance between the pupils
export const EYE_HALF = 0.275;     // half an eye's length; a head is five eyes wide
export const MOUTH_HALF = 0.40;

/**
 * How far the head may turn, in radians.
 *
 * The head is a front shell, not a closed surface, and past this angle its open edge
 * swings into view and the face reads as a mask on a stick. The sampling arc is derived
 * from this constant, so widening one widens the other.
 */
export const MAX_TURN = 0.38;
const PULSE_RINGS = 3;     // concentric pulses that travel outward while listening

/**
 * The head's outline, as half-width and half-depth at a given height.
 *
 * **This table is why the avatar is a head and not a ball.** A sphere or an ellipsoid has
 * a circular silhouette, and from the front the silhouette *is* the face — no amount of
 * relief on the surface changes it, because relief is strongest at the centre and fades
 * to nothing exactly at the edge. So the outline is stated directly instead.
 *
 * The shape a viewer recognises: a rounded cranium, a slight pinch at the temples, the
 * widest point at the cheekbones, then a **straight run down to the jaw angle** and a
 * sharper taper to a narrow chin. That straight run and the corner at the gonion are what
 * an ellipse cannot produce and what makes a jaw look like a jaw.
 *
 * Half-depth follows separately, because a head is much shallower at the crown and the
 * chin than through the cheeks.
 */
const HEAD_OUTLINE = [
  // y      width  depth
  [1.460, 0.205, 0.34],   // crown — flat on top, as a skull is, not domed to a point
  [1.355, 0.480, 0.60],
  [1.240, 0.660, 0.74],
  [1.100, 0.782, 0.845],
  [0.940, 0.848, 0.90],
  [0.760, 0.922, 0.955],  // parietal — the skull is nearly at its widest here
  [0.560, 0.938, 0.985],
  [0.420, 0.906, 1.000],  // TEMPLE: a pinch, and a local minimum an ellipse cannot have
  [0.250, 0.934, 1.000],
  [0.060, 1.000, 0.990],  // ZYGOMATIC: the cheekbone, the widest point of a face
  [-0.130, 0.986, 0.975],
  [-0.330, 0.942, 0.945],
  [-0.530, 0.886, 0.905],
  [-0.700, 0.836, 0.870],  // the near-straight run down the masseter
  [-0.864, 0.780, 0.830],  // GONION: the corner, where the slope trebles
  [-1.020, 0.682, 0.775],
  [-1.170, 0.578, 0.715],  // the straight run in to the chin
  [-1.300, 0.478, 0.650],
  [-1.410, 0.396, 0.585],
  [-1.480, 0.330, 0.535],
  [-1.520, 0.258, 0.500],  // CHIN: a flat plate a quarter of the face wide.
                           // Taper this to a point and the head ends in a beak — which
                           // is what the first render of this table actually did.
];

/** Linear interpolation down the outline table. */
function outlineAt(y, column) {
  if (y >= HEAD_OUTLINE[0][0]) return HEAD_OUTLINE[0][column];
  const last = HEAD_OUTLINE[HEAD_OUTLINE.length - 1];
  if (y <= last[0]) return last[column];
  for (let i = 0; i < HEAD_OUTLINE.length - 1; i += 1) {
    const [upperY] = HEAD_OUTLINE[i];
    const [lowerY] = HEAD_OUTLINE[i + 1];
    if (y <= upperY && y >= lowerY) {
      const k = (upperY - y) / (upperY - lowerY);
      return HEAD_OUTLINE[i][column] * (1 - k) + HEAD_OUTLINE[i + 1][column] * k;
    }
  }
  return last[column];
}

/**
 * Width and depth scales.
 *
 * A head is **1.4 times as tall as it is wide**, and getting that ratio wrong is most of
 * why a first attempt looks like a ball: at 1.2 the outline is within a few percent of a
 * circle at every height, and no feature drawn inside it can undo that. Height here is
 * fixed at 2.98, so the half-width follows from the ratio rather than from taste.
 */
const WIDTH_SCALE = (CROWN_Y - CHIN_Y) / 1.44 / 2;
const DEPTH_SCALE = 1.12;   // deeper than it is wide, as a skull is

export const headHalfWidth = (y) => outlineAt(y, 1) * WIDTH_SCALE;
export const headHalfDepth = (y) => outlineAt(y, 2) * DEPTH_SCALE;

/**
 * The surface, so a feature can be placed *on* the head rather than guessed near it.
 *
 * Hard-coded depths were how the eyes and lips ended up floating in front of the mesh
 * whenever the head shape changed. Everything now asks the same function.
 */
export function surfaceZ(x, y) {
  const halfWidth = headHalfWidth(y);
  const ratio = Math.min(1, Math.abs(x) / Math.max(halfWidth, 1e-3));
  const across = Math.sqrt(Math.max(0, 1 - ratio * ratio));
  return headHalfDepth(y) * across + sculptFace(x, y) * across;
}

/**
 * A 2D bump. The building block of the sculpt: everything anatomical here is a smooth
 * hill or hollow, and summing them is what avoids visible seams between features.
 */
export function bump(dx, dy, sx, sy) {
  return Math.exp(-((dx * dx) / (2 * sx * sx) + (dy * dy) / (2 * sy * sy)));
}

/**
 * How far the surface moves at (x, y), in the view direction.
 *
 * Coordinates come from the landmarks at the top of the file, so a correction to the eye
 * line moves the sockets, the cheekbones and the nose with it rather than leaving them
 * where they were.
 *
 * The values are anatomical rather than arbitrary. The brow **overhangs** the eyes and
 * the sockets sit **behind** it — that pairing is what the eye reads as a face, more than
 * the eyes themselves. The cheekbones and chin then carry the light, and the philtrum and
 * jaw hollows stop the lower half looking inflated.
 */
export function sculptFace(x, y) {
  const ax = Math.abs(x);
  let d = 0;

  d += 0.17 * bump(ax - 0.46, y - BROW_Y, 0.30, 0.11);            // brow ridge, overhanging
  d -= 0.21 * bump(ax - EYE_X, y - EYE_Y, 0.21, 0.12);            // socket, recessed behind it
  d += 0.14 * bump(ax - 0.70, y - EYE_Y + 0.22, 0.23, 0.19);      // cheekbone
  d -= 0.08 * bump(ax - 0.72, y - EYE_Y + 0.62, 0.21, 0.21);      // hollow beneath it
  d += 0.17 * bump(x, y - EYE_Y - 0.10, 0.10, 0.24);              // nose bridge
  d += 0.36 * bump(x, y - NOSE_BASE_Y - 0.06, 0.13, 0.12);        // tip — the frontmost point
  d -= 0.07 * bump(x, y - NOSE_BASE_Y + 0.12, 0.09, 0.08);        // philtrum
  d += 0.07 * bump(x, y - MOUTH_Y, 0.30, 0.08);                   // lips
  d -= 0.06 * bump(x, y - MOUTH_Y + 0.16, 0.26, 0.07);            // crease under the lower lip
  d += 0.19 * bump(x, y + 1.24, 0.28, 0.18);                      // chin
  d -= 0.09 * bump(ax - 0.92, y - BROW_Y - 0.28, 0.16, 0.26);     // temples

  return d;
}
