import { describe, expect, it } from 'vitest';
import {
  BAR_GAP,
  BAR_W,
  barsWidth,
  barWidth,
  barX,
  CHEVRON_HALF_H,
  CHEVRON_HALF_W,
  chevronPath,
  crossPath,
  dashArray,
  DIAMOND_HALO_HALF,
  DOT_R,
  GLYPH_SCALE,
  glyphSize,
  HALO_DOT_R,
  HALO_R,
  hatchLines,
  RING_R,
  RING_X_HALF,
  SQUARE_HALF,
  STROKE,
  strokeWidth,
  X_HALF
} from './markGeometry';
import { MARK_SIZES } from './status';

/**
 * The marks frontend/design/factory-ui/ actually draws, transcribed from the
 * SVG in the artboards rather than eyeballed:
 *
 * - DEpic.html:50 — the 12px legend (`<svg width="12" height="12">`): todo ring
 *   r=3.84 stroke 1.5, in-progress halo r=6.0 + dot r=3.6, in-review ring
 *   r=3.84 stroke 2, done dot r=4.32, failed cross from 2.88 to 9.12 stroke
 *   2.2, held ring r=3.84 stroke 2, blocked square x=1.92 w=8.16 stroke 1.5,
 *   cancelled ring r=3.84 stroke 1.2 + cross 3.84→8.16, needs-you diamond
 *   x=-0.12 w=12.24 over square x=1.92 w=8.16.
 * - DEpic.html:64 — the 10px "8 done" grid (`<svg width="10" height="10">`):
 *   done dot cx=5 cy=5 r=3.6. This is the one that catches a glyph scale at
 *   size 10: any scale >1 draws a dot wider than the artboard's.
 * - The 14px and 18px marks elsewhere in DEpic.html: r=4.48 / r=5.04 at 14,
 *   halo r=9.0 + dot r=5.4 at 18.
 */
const ARTBOARD_RADII: [size: number, fraction: number, r: number][] = [
  [10, DOT_R, 3.6],
  [12, RING_R, 3.84],
  [12, DOT_R, 4.32],
  [12, HALO_R, 6.0],
  [12, HALO_DOT_R, 3.6],
  [14, RING_R, 4.48],
  [14, DOT_R, 5.04],
  [18, HALO_R, 9.0],
  [18, HALO_DOT_R, 5.4]
];

describe('glyphSize — the box a mark is drawn into', () => {
  it.each(ARTBOARD_RADII)(
    'size %d × fraction %f is the artboard radius %f',
    (size, fraction, r) => {
      expect(glyphSize(size) * fraction).toBeCloseTo(r, 6);
    }
  );

  it('10, 12, 14 and 18 take no scale — the artboards give those marks air', () => {
    for (const size of [10, 12, 14, 18]) {
      expect(GLYPH_SCALE[size], `size ${size}`).toBeUndefined();
      expect(glyphSize(size)).toBe(size);
    }
  });

  it('a 7px mark fills its box, because DWork draws its row marks edge to edge', () => {
    // DWork's state marks are CSS circles: `width: 7px; height: 7px;
    // border-radius: 50%` filled, and the same box with `border: 1px solid;
    // box-sizing: border-box` hollow — both 7px across. The scale brings the
    // SVG dot and the SVG ring's outer edge within half a pixel of that;
    // nothing smaller reads as a mark at all at this size.
    const dotDiameter = 2 * glyphSize(7) * DOT_R;
    const ringOuter = 2 * glyphSize(7) * RING_R + strokeWidth('thin', 7);
    expect(7 - dotDiameter).toBeLessThan(0.5);
    expect(7 - dotDiameter).toBeGreaterThan(0);
    expect(Math.abs(7 - ringOuter)).toBeLessThan(0.5);
  });

  it('an off-scale size still draws, at its own box', () => {
    expect(glyphSize(9)).toBe(9);
  });
});

describe('strokeWidth', () => {
  it('has a weight for every MARK_SIZES entry — a new size cannot ship bare', () => {
    for (const weight of Object.keys(STROKE) as (keyof typeof STROKE)[]) {
      for (const size of MARK_SIZES) {
        expect(STROKE[weight][size], `${weight} at ${size}`).toBeGreaterThan(0);
      }
    }
  });

  it('is the artboards weights at 12: 1.5 thin, 2 thick, 2.2 cross, 1.2 faint', () => {
    expect(strokeWidth('thin', 12)).toBe(1.5);
    expect(strokeWidth('thick', 12)).toBe(2);
    expect(strokeWidth('cross', 12)).toBe(2.2);
    expect(strokeWidth('faint', 12)).toBe(1.2);
  });

  it('grows monotonically with size for every weight', () => {
    for (const weight of Object.keys(STROKE) as (keyof typeof STROKE)[]) {
      const widths = MARK_SIZES.map((s) => strokeWidth(weight, s));
      for (let i = 1; i < widths.length; i++) {
        expect(widths[i], `${weight} at ${MARK_SIZES[i]}`).toBeGreaterThanOrEqual(widths[i - 1]);
      }
    }
  });

  it('falls back to a ratio for a size with no table entry', () => {
    expect(strokeWidth('thin', 9)).toBeCloseTo(9 * 0.125, 6);
    expect(strokeWidth('thin', 2)).toBe(1);
  });
});

describe('crossPath', () => {
  it('is DEpic.html:50s failed X at size 12: 2.88 → 9.12', () => {
    const hi = 9.120000000000001; // 6 + 12 × 0.26, as the artboard emits it
    expect(crossPath(12, X_HALF)).toBe(`M 2.88 2.88 L ${hi} ${hi} M ${hi} 2.88 L 2.88 ${hi}`);
  });

  it("is the cancelled ring's inner cross at size 12: 3.84 → 8.16", () => {
    expect(crossPath(12, RING_X_HALF)).toBe('M 3.84 3.84 L 8.16 8.16 M 8.16 3.84 L 3.84 8.16');
  });

  it('stays centred on the box at every size', () => {
    for (const size of MARK_SIZES) {
      const [, lo, , , hi] = crossPath(size, X_HALF).split(' ');
      expect(Number(lo) + Number(hi), `size ${size}`).toBeCloseTo(size, 6);
    }
  });
});

describe('hatchLines — the blocked square', () => {
  it('is five lines of slope −1 stepped through the square centre', () => {
    const lines = hatchLines(12);
    expect(lines).toHaveLength(5);
    // x + y = k, stepping by glyph × HATCH_STEP = 2.04 either side of k = 12.
    const ks = lines.map((l) => l.x1 + l.y1);
    expect(ks).toEqual([7.92, 9.96, 12, 14.04, 16.08]);
    for (const l of lines) expect(l.x2 + l.y2).toBeCloseTo(l.x1 + l.y1, 6);
  });

  it('never escapes the square it hatches', () => {
    for (const size of MARK_SIZES) {
      const lo = size / 2 - glyphSize(size) * SQUARE_HALF;
      const hi = size / 2 + glyphSize(size) * SQUARE_HALF;
      for (const l of hatchLines(size)) {
        for (const v of [l.x1, l.y1, l.x2, l.y2]) {
          expect(v, `size ${size}`).toBeGreaterThanOrEqual(lo - 1e-9);
          expect(v, `size ${size}`).toBeLessThanOrEqual(hi + 1e-9);
        }
      }
    }
  });
});

describe('bars — the one mark wider than tall', () => {
  it('is three 3/7 bars with 2/7 gaps, 13/7 of the box wide', () => {
    expect(barsWidth(14)).toBeCloseTo(14 * (13 / 7), 2);
    expect(barX(14, 0)).toBe(0);
    expect(barX(14, 1)).toBeCloseTo(10, 6);
    expect(barWidth(14)).toBeCloseTo(6, 6);
  });

  it('leaves no bar hanging past the svg width it is given', () => {
    for (const size of MARK_SIZES) {
      expect(barX(size, 2) + barWidth(size), `size ${size}`).toBeLessThanOrEqual(
        barsWidth(size) + 0.01
      );
    }
  });

  it('BAR_W and BAR_GAP are the artboards 3px and 2px at the 7px row size', () => {
    expect(7 * BAR_W).toBeCloseTo(3, 6);
    expect(7 * BAR_GAP).toBeCloseTo(2, 6);
  });
});

describe('dashArray — the pending ring', () => {
  it('reads as dashed, not dotted: ~8 dash+gap periods around a 12px ring', () => {
    const [on, off] = dashArray(12).split(' ').map(Number);
    const circumference = 2 * Math.PI * glyphSize(12) * RING_R;
    expect(circumference / (on + off)).toBeGreaterThan(7);
    expect(circumference / (on + off)).toBeLessThan(11);
  });

  it('scales with the glyph, so a 7px ring keeps the same dash count', () => {
    const period = (size: number) => {
      const [on, off] = dashArray(size).split(' ').map(Number);
      return on + off;
    };
    expect(period(7) / glyphSize(7)).toBeCloseTo(period(18) / glyphSize(18), 6);
  });
});

describe('the diamond halo', () => {
  it('is DEpic.html:50s needs-you diamond at 12: x=-0.12, w=12.24 over 1.92/8.16', () => {
    const c = 6;
    expect(c - glyphSize(12) * DIAMOND_HALO_HALF).toBeCloseTo(-0.12, 6);
    expect(glyphSize(12) * DIAMOND_HALO_HALF * 2).toBeCloseTo(12.24, 6);
    expect(c - glyphSize(12) * SQUARE_HALF).toBeCloseTo(1.92, 6);
    expect(glyphSize(12) * SQUARE_HALF * 2).toBeCloseTo(8.16, 6);
  });

  it('reaches outside its box — which is why the svg never clips', () => {
    expect(DIAMOND_HALO_HALF).toBeGreaterThan(0.5);
  });
});

describe('chevronPath — the staleness badge (#2303)', () => {
  /** The three points of the chevron at one size, as `[x, y]` pairs. */
  const points = (size: number): [number, number][] => {
    const parts = chevronPath(size).split(' ');
    // "M x y L x y L x y" — the command letters sit at 0, 3 and 6.
    return [
      [Number(parts[1]), Number(parts[2])],
      [Number(parts[4]), Number(parts[5])],
      [Number(parts[7]), Number(parts[8])]
    ];
  };

  it('is an OPEN three-point path, never a closed shape', () => {
    // DESIGN.md's Don'ts rule out filled colour blocks, and the badge is drawn
    // with `fill="none"`: a path that closed itself would read as a triangle
    // the moment anything gave it a fill.
    expect(chevronPath(12)).toMatch(/^M [\d.]+ [\d.]+ L [\d.]+ [\d.]+ L [\d.]+ [\d.]+$/);
    expect(chevronPath(12)).not.toContain('Z');
  });

  it('points RIGHT: the middle vertex is the rightmost and vertically centred', () => {
    // The direction is the meaning — an arrowhead pointing the way the head
    // went. A chevron pointing anywhere else is a different mark.
    for (const size of MARK_SIZES) {
      const [top, tip, bottom] = points(size);
      expect(tip[0], `size ${size}`).toBeGreaterThan(top[0]);
      expect(tip[0], `size ${size}`).toBeGreaterThan(bottom[0]);
      expect(tip[1], `size ${size}`).toBeCloseTo(size / 2, 6);
      expect(top[0], `size ${size}`).toBeCloseTo(bottom[0], 6);
    }
  });

  it('stays centred on its box at every size', () => {
    for (const size of MARK_SIZES) {
      const [top, , bottom] = points(size);
      expect(top[1] + bottom[1], `size ${size}`).toBeCloseTo(size, 6);
    }
  });

  it('is narrower than it is tall, so it never reads as the needs-you diamond', () => {
    // The diamond is rationed to "a person is the next move" (DESIGN.md), and
    // a chevron as wide as it is tall is half of one.
    expect(CHEVRON_HALF_W).toBeLessThan(CHEVRON_HALF_H);
    for (const size of MARK_SIZES) {
      const [top, tip, bottom] = points(size);
      expect(tip[0] - top[0], `size ${size}`).toBeLessThan(bottom[1] - top[1]);
    }
  });

  it('is the fractions it declares, measured off the glyph not the box', () => {
    // The 7px dot grows its glyph back to the box (GLYPH_SCALE), and a chevron
    // measured off the box instead would be the one mark that shrinks there.
    for (const size of MARK_SIZES) {
      const [top, tip] = points(size);
      expect(size / 2 - top[1], `size ${size}`).toBeCloseTo(glyphSize(size) * CHEVRON_HALF_H, 6);
      expect(tip[0] - size / 2, `size ${size}`).toBeCloseTo(glyphSize(size) * CHEVRON_HALF_W, 6);
    }
  });

  it('stays inside its viewBox at every size, 7px included', () => {
    // The 7px dot is the tight one: its glyph is grown back past the box
    // (GLYPH_SCALE), so a half-extent over 0.5/1.3 would put the chevron's
    // point outside the `0 0 size size` viewBox the badge draws it in.
    for (const size of MARK_SIZES) {
      for (const [x, y] of points(size)) {
        expect(x, `size ${size}`).toBeGreaterThanOrEqual(0);
        expect(y, `size ${size}`).toBeGreaterThanOrEqual(0);
        expect(x, `size ${size}`).toBeLessThanOrEqual(size);
        expect(y, `size ${size}`).toBeLessThanOrEqual(size);
      }
    }
  });
});
