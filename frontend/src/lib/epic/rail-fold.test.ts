/**
 * #2333 (round 2) — the width the rail unfolds its tracks at must be a width
 * that can hold them.
 *
 * The first fix read the overprint as a phone bug and folded the rail at `sm`.
 * It is not a phone bug: three of the four tracks are fixed, so the rail needs
 * 1240px of window before the artboard's layout fits, and at 641px — one pixel
 * above that fold — "Pipeline · main" was printed over "Evidence" again, all
 * the way up to ~1230px. A browser check that only visited 390px saw none of
 * it.
 *
 * So the breakpoints are arithmetic over `RAIL_TRACKS`, and this file is that
 * arithmetic plus the one thing a model test cannot see: that the stylesheet
 * and the Tailwind variants in `SelectedRail.svelte` fold on the same two
 * numbers the arithmetic allows. It reads the component as text because those
 * numbers live in a `@media` query and a `max-lg:` class, neither of which
 * reaches an SSR render or jsdom (which has no layout at all). The pixels
 * themselves are read in a real browser by `scripts/ui-check.mjs`, at both
 * sides of both folds.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { RAIL_TRACKS } from './rail';

const SOURCE = readFileSync(
  fileURLToPath(new URL('./SelectedRail.svelte', import.meta.url)),
  'utf8'
);

/** The same source with its prose taken out: the file explains these variants
 *  in its own comments, and a comment is not a rule the browser folds on. */
const CODE = SOURCE.replace(/\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->/g, '');

/** Tailwind's own breakpoints, the only two the rail is allowed to fold on. */
const TAILWIND = { sm: 640, md: 768, lg: 1024, xl: 1280 } as const;

const rem = (px: number) => `${px / 16}rem`;

/** Every width the component's own stylesheet mentions in a media query. */
function queried(): number[] {
  return [...CODE.matchAll(/\(width [<>]=? ([\d.]+)(rem|px)\)/g)]
    .map((m) => (m[2] === 'rem' ? Number(m[1]) * 16 : Number(m[1])))
    .sort((a, b) => a - b);
}

describe('the rail folds where its tracks stop fitting (#2333)', () => {
  it('needs more than `sm` for the artboard: the first fix folded 600px early', () => {
    // The sum the four-track layout cannot go under: the page gutter either
    // side, the three fixed tracks, and the fluid Pipeline track's minimum.
    const needed =
      2 * RAIL_TRACKS.gutter +
      RAIL_TRACKS.selected +
      RAIL_TRACKS.evidence +
      RAIL_TRACKS.attention +
      RAIL_TRACKS.pipeline;
    expect(needed).toBeGreaterThan(TAILWIND.sm);
    expect(RAIL_TRACKS.fourUp).toBeGreaterThanOrEqual(needed);
  });

  it('gives the Pipeline track its minimum in the 2×2 tier too', () => {
    // Two equal tracks, so the narrowest window in the tier sets the column.
    const column = (RAIL_TRACKS.twoUp - 2 * RAIL_TRACKS.gutter) / 2;
    expect(column).toBeGreaterThanOrEqual(RAIL_TRACKS.pipeline);
    // ...and a tier is only worth having if it is narrower than the one above.
    expect(RAIL_TRACKS.twoUp).toBeLessThan(RAIL_TRACKS.fourUp);
  });

  it('folds on Tailwind breakpoints, so the classes and the queries can agree', () => {
    expect(RAIL_TRACKS.twoUp).toBe(TAILWIND.lg);
    expect(RAIL_TRACKS.fourUp).toBe(TAILWIND.xl);
  });

  it('folds the stylesheet on exactly those two widths, and on no other', () => {
    // Two tiers below the artboard's: the 2×2 one is BOUNDED at both ends, so
    // the stacked tier under it is reachable whatever order they are written
    // in — an open-ended `width < fourUp` above it would swallow the lot.
    expect(CODE).toContain(
      `@media (width >= ${rem(RAIL_TRACKS.twoUp)}) and (width < ${rem(RAIL_TRACKS.fourUp)})`
    );
    expect(CODE).toContain(`@media (width < ${rem(RAIL_TRACKS.twoUp)})`);
    expect(queried()).toEqual([RAIL_TRACKS.twoUp, RAIL_TRACKS.twoUp, RAIL_TRACKS.fourUp]);
  });

  it('folds the columns on the stacking width, and on no other', () => {
    // Every responsive variant the rail's own markup carries, by breakpoint.
    // The 2×2 tier is not here on purpose: its rules are positional, because a
    // column the model drops moves every section after it into another cell.
    const variants = [...CODE.matchAll(/(?:^|[\s'"`])((?:(?:max-)?(?:sm|md|lg|xl|2xl):)+)[a-z-]/g)]
      .map((m) => m[1])
      .filter((v) => v !== 'sm:'); // the section gutter and TAP_PILL, not a fold
    expect(variants.length).toBeGreaterThan(3);
    expect([...new Set(variants)]).toEqual(['max-lg:']);
  });

  it('places the 2×2 tier by position, not by which column it is', () => {
    // `:nth-child(odd)` is the first column of a row and `(n + 3)` is every
    // row after the first, over however many sections the model drew.
    const tier = CODE.slice(
      CODE.indexOf(`(width >= ${rem(RAIL_TRACKS.twoUp)})`),
      CODE.indexOf(`@media (width < ${rem(RAIL_TRACKS.twoUp)})`)
    );
    expect(tier).toContain('repeat(2, minmax(0, 1fr))');
    expect(tier).toContain(':nth-child(odd)');
    expect(tier).toContain(':nth-child(n + 3)');
  });
});
