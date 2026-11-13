/**
 * D18 (design review C2) — the epic surface had seven type sizes.
 *
 * DESIGN.md allows five (11, 13, 15, 26, 44) and says so twice: "Five type
 * sizes, no more" and "Don't add a sixth type size". The epic page, its
 * canvas, its cards, its popover and its Gantt had grown 16, 14, 13.5, 12 and
 * 10px as well — each one reasonable where it was written, and together a
 * scale nobody can read a hierarchy off.
 *
 * A scan rather than a list of assertions: the next size drifts in the same
 * way the last five did, one sensible local decision at a time.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/** DESIGN.md's `typography` block, in pixels. */
const SCALE = [11, 13, 15, 26, 44];

const HERE = fileURLToPath(new URL('.', import.meta.url));
const PAGE = fileURLToPath(
  new URL('../../routes/(factory)/epics/[ref]/+page.svelte', import.meta.url)
);

/** Every component of the epic surface, plus the page that composes them. */
const SURFACE = [
  ...readdirSync(HERE)
    .filter((f) => f.endsWith('.svelte'))
    .map((f) => [`$lib/epic/${f}`, `${HERE}${f}`] as const),
  ['epics/[ref]/+page.svelte', PAGE] as const
];

/** Every type size a file sets: CSS `font-size`, an SVG `font-size` attribute,
 * and Tailwind's arbitrary `text-[Npx]`. */
function sizes(source: string): number[] {
  const found: number[] = [];
  for (const m of source.matchAll(/font-size:\s*([\d.]+)px/g)) found.push(Number(m[1]));
  for (const m of source.matchAll(/font-size="([\d.]+)"/g)) found.push(Number(m[1]));
  for (const m of source.matchAll(/text-\[([\d.]+)px\]/g)) found.push(Number(m[1]));
  return found;
}

describe('the epic surface has five type sizes (D18)', () => {
  it('scans something: a glob that matched nothing would pass silently', () => {
    expect(SURFACE.length).toBeGreaterThan(5);
    expect(SURFACE.flatMap(([, path]) => sizes(readFileSync(path, 'utf8'))).length).toBeGreaterThan(
      10
    );
  });

  it.each(SURFACE)('%s', (_name, path) => {
    const off = [...new Set(sizes(readFileSync(path, 'utf8')))].filter(
      (px) => !SCALE.includes(px)
    );
    expect(off).toEqual([]);
  });
});
