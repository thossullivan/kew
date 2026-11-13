/**
 * The colour maths behind browser-check's small-text contrast check (#3353),
 * kept out of the page so it can be tested without a browser.
 *
 * A colour is straight sRGB 0-255 with alpha 0-1: `{ r, g, b, a }`. A text
 * node is described by its fill and by the elements from its parent up to the
 * root, each with its background and its opacity.
 */

/** Small text needs 4.5:1 under WCAG AA. */
export const AA = 4.5;

const CLEAR = { r: 0, g: 0, b: 0, a: 0 };

/** `top` composited over `under`, source-over. */
export function over(top, under) {
  const a = top.a + under.a * (1 - top.a);
  if (a === 0) return CLEAR;
  const mix = (k) => (top[k] * top.a + under[k] * under.a * (1 - top.a)) / a;
  return { r: mix('r'), g: mix('g'), b: mix('b'), a };
}

/** WCAG relative luminance. */
export function luminance(c) {
  const lin = (v) => {
    const s = v / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
}

/** WCAG contrast ratio of two opaque colours. */
export function ratio(x, y) {
  const [hi, lo] = [luminance(x), luminance(y)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

/** What a pixel under `paint` shows once every element from the text's parent
 *  up to the root has drawn: each lays its content over its own background,
 *  then fades the lot by its opacity, and the page colour sits under all. */
function rendered(paint, chain, page) {
  let colour = paint;
  for (const { background, opacity } of chain) {
    colour = over(colour, background);
    if (opacity < 1) colour = { ...colour, a: colour.a * opacity };
  }
  return over(colour, { ...page, a: 1 });
}

/** The text's colour and the colour behind it as a user sees them, and the
 *  ratio between the two; null when an opacity of 0 on the way up hides it.
 *  `chain` runs from the text's parent element to the root. */
export function textContrast(fill, chain, page) {
  if (chain.some((step) => step.opacity <= 0)) return null;
  const fg = rendered(fill, chain, page);
  const bg = rendered(CLEAR, chain, page);
  return { fg, bg, ratio: ratio(fg, bg) };
}

/** A colour as #rrggbb. */
export function hex(c) {
  return '#' + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
}
