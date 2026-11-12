// The numbers behind a status mark, with no SVG in sight (FUI-04).
//
// StatusMark.svelte used to hold these inline, which meant the only check on
// them was a human reading the artboard — and one wrong entry (GLYPH_SCALE at
// size 10) survived exactly that. They live here so markGeometry.test.ts can
// assert them against the marks frontend/design/factory-ui/ actually draws.
//
// Every extent is measured off the DEpic legend (y ≈100–115), DEpic's 10px
// "done" grid, and the DWork state column (x 324–392).

/** Extents as fractions of the glyph, from the legend's 12px marks: a ring is
 * drawn on r = 0.32 and a filled dot on 0.36, so the two read as the same size
 * once the stroke is accounted for. */
export const RING_R = 0.32;
export const DOT_R = 0.36;
export const HALO_R = 0.5;
export const HALO_DOT_R = 0.3;
export const X_HALF = 0.26;
export const RING_X_HALF = 0.18;
export const SQUARE_HALF = 0.34;
export const HATCH_STEP = 0.17;
export const DIAMOND_HALO_HALF = 0.51;
/** The DWork multi-task bars are 3×7 with a 2px gap, so they scale off 1/7. */
export const BAR_W = 3 / 7;
export const BAR_GAP = 2 / 7;
/** The dashed ring's dash and gap, as fractions of the glyph. The artboards
 * draw no dashed ring — these are chosen so a 12px ring carries ~8 dashes,
 * which is the smallest count that still reads as dashed rather than dotted. */
export const DASH_ON = 0.13;
export const DASH_OFF = 0.12;

/**
 * Sizes whose glyph is grown back to fill its box.
 *
 * A 10px+ mark is given air inside its box — the legend's 12px ring is 9px
 * across, and DEpic's 10px done-dot grid is drawn at r = 3.6 = 10 × DOT_R, so
 * 10 takes no scale either. At 7 there is no room for that air: DWork's row
 * marks are 7px CSS circles (`width: 7px; border-radius: 50%`) that fill their
 * box edge to edge, so the glyph grows back to the box there and only there.
 */
export const GLYPH_SCALE: Record<number, number> = { 7: 1.3 };

export type StrokeWeight = 'faint' | 'thin' | 'thick' | 'cross';

/** Stroke weights per size rather than per fraction: the artboards draw 1px at
 * 7px and 1.5px at 12–14px, which no single ratio reproduces. */
export const STROKE: Record<StrokeWeight, Record<number, number>> = {
  faint: { 7: 1, 10: 1, 12: 1.2, 14: 1.2, 18: 1.5 },
  thin: { 7: 1, 10: 1.2, 12: 1.5, 14: 1.5, 18: 2 },
  thick: { 7: 1.2, 10: 1.6, 12: 2, 14: 2, 18: 2.5 },
  cross: { 7: 1.4, 10: 1.8, 12: 2.2, 14: 2.4, 18: 3 }
};

/** The drawable extent of a mark at `size` — the box, grown where the artboards
 * grow it. */
export function glyphSize(size: number): number {
  return size * (GLYPH_SCALE[size] ?? 1);
}

/** The stroke width for one weight at one size, with a ratio fallback so an
 * off-scale size still draws. */
export function strokeWidth(weight: StrokeWeight, size: number): number {
  return STROKE[weight][size] ?? Math.max(1, size * 0.125);
}

/** A diagonal cross reaching `half` of the glyph either side of centre. */
export function crossPath(size: number, half: number): string {
  const c = size / 2;
  const glyph = glyphSize(size);
  const lo = c - glyph * half;
  const hi = c + glyph * half;
  return `M ${lo} ${lo} L ${hi} ${hi} M ${hi} ${lo} L ${lo} ${hi}`;
}

export type HatchLine = { x1: number; y1: number; x2: number; y2: number };

/** The five 45° hatch lines of the blocked square, clipped to the square. */
export function hatchLines(size: number): HatchLine[] {
  const c = size / 2;
  const glyph = glyphSize(size);
  const lo = c - glyph * SQUARE_HALF;
  const hi = c + glyph * SQUARE_HALF;
  const step = glyph * HATCH_STEP;
  // A line of slope −1 is x + y = k; the square's centre line is k = 2c.
  return [-2, -1, 0, 1, 2].map((i) => {
    const k = 2 * c + i * step;
    const x1 = Math.max(lo, k - hi);
    const x2 = Math.min(hi, k - lo);
    return { x1, y1: k - x1, x2, y2: k - x2 };
  });
}

/** Bars are the one mark wider than tall: three 3×7s with 2px gaps. */
export function barsWidth(size: number): number {
  return Math.round(size * (3 * BAR_W + 2 * BAR_GAP) * 100) / 100;
}

/** The left edge of bar `i` of three. */
export function barX(size: number, i: number): number {
  return i * size * (BAR_W + BAR_GAP);
}

/** The `stroke-dasharray` for a dashed ring at `size`. */
export function dashArray(size: number): string {
  const glyph = glyphSize(size);
  return `${glyph * DASH_ON} ${glyph * DASH_OFF}`;
}

/** The width of one of the three multi-task bars. */
export function barWidth(size: number): number {
  return size * BAR_W;
}

/**
 * The not-ready mark's filled half: the left half of the ring's own disc, so
 * the outline and the fill share one edge (#3466). A round that ended against
 * the change is half way, neither the done dot nor an empty ring ahead.
 */
export function halfDiscPath(size: number): string {
  const c = size / 2;
  const r = glyphSize(size) * RING_R;
  return `M ${c} ${c - r} A ${r} ${r} 0 0 0 ${c} ${c + r} Z`;
}

/** The staleness badge's chevron, as a fraction of the glyph (#2303).
 *
 * A new mark needs a new SHAPE, never a reused one (DESIGN.md "Status marks"),
 * and every geometry above is a ring, a dot, a cross, a square, a diamond or
 * bars. The chevron is the one remaining primitive that reads as *moved on*:
 * an open arrowhead pointing the way the head went. Narrower than it is tall,
 * so it never reads as the diamond rationed for "a person is the next move".
 */
export const CHEVRON_HALF_H = 0.3;
export const CHEVRON_HALF_W = 0.19;

/**
 * An open chevron pointing right, centred in `size`.
 *
 * Drawn as a stroke rather than a fill: DESIGN.md's Don'ts rule out filled
 * colour blocks, and an outline keeps the badge quiet in `--text-3` while the
 * same path reads as alarm in `--accent` without changing weight.
 */
export function chevronPath(size: number): string {
  const c = size / 2;
  const glyph = glyphSize(size);
  const dy = glyph * CHEVRON_HALF_H;
  const dx = glyph * CHEVRON_HALF_W;
  return `M ${c - dx} ${c - dy} L ${c + dx} ${c} L ${c - dx} ${c + dy}`;
}
