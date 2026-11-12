/**
 * #2210 — the Epics list says the drain caption once, over the column.
 *
 * The regression this guards is the one the owner's screenshots caught: the
 * page printing `epic.label` beside every `epic_ref`, which is the drain
 * figure's caption and not the epic's name, so every row read "drain spend".
 *
 * There is no component renderer in this suite (see epic/render.ts), so the
 * assertions are against the page source, the same way page.shelves.test.ts
 * and page.strip.test.ts hold their surfaces to one reading.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { dataRowClasses } from '$lib/factory/classes';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');
/** The one stylesheet that decides what a `.text-11` label looks like. */
const css = readFileSync(new URL('../../../app.css', import.meta.url), 'utf8');

describe('the epics list drain column', () => {
  it('heads the column through the shared reading, not its own', () => {
    expect(page).toMatch(
      /import \{ DRAIN_CAPTION, DRAIN_COLUMN, drainTitle \} from '\$lib\/epic\/drain'/
    );
  });

  it('heads the column with one word and keeps the caption as its title (#2787 N22)', () => {
    // "drain spend" wrapped onto two lines in the 88px track; every other
    // head is one word, and the figure's caption is still on the head —
    // "drain spend" on every roster, whatever the rows carry (#2844 P9).
    const head = markup.slice(markup.indexOf('snippet headRow'), markup.indexOf('snippet shelf'));
    expect(head).toContain('title={DRAIN_CAPTION}>{DRAIN_COLUMN}</span>');
  });

  it('leaves the capital to the head row’s own type class', () => {
    // The caption is the API's word in lower case, and what capitalises it is
    // `.text-11`'s `text-transform` on the head row — inherited, so it reaches
    // the whole caption. A `first-letter:uppercase` beside it was inert, and
    // pinning it here claimed a mechanism that was not the one in effect.
    const head = markup.slice(markup.indexOf('snippet headRow'), markup.indexOf('snippet shelf'));
    expect(head).not.toContain('first-letter:uppercase');
    expect(dataRowClasses({ head: true })).toContain('text-11');
    expect(css).toMatch(/(?:\.|@utility\s+)text-11\s*\{[^}]*text-transform:\s*uppercase/);
  });

  it('never prints the drain caption as a row\'s own text', () => {
    // `{… epic.label}` rendered as text in a row is exactly the old behaviour.
    // The `title` is the one place it may still appear — see below.
    const rows = markup.slice(markup.indexOf('snippet shelf'));
    expect(rows).not.toMatch(/>\s*\{[^}]*epic\.label/);
    expect(rows).not.toMatch(/\{epic\.label\}/);
  });

  it('keeps a row caption the heading could not carry, as a title', () => {
    expect(markup).toContain('title={drainTitle(DRAIN_CAPTION, epic.label)}');
  });

  it('still names the retirement reason where a label used to sit', () => {
    // #2206's reading is not what this issue removes: an archived epic's own
    // sentence is about the epic, and stays beside its ref.
    expect(markup).toContain('{retirement(epic)}');
  });
});
