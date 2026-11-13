/**
 * #2210 — the Work detail pane before an issue is chosen.
 *
 * It was one sentence over two thirds of the screen. It now shows the
 * needs-you queue summary: the count the header counts, the rows it is made
 * of, and a way into each one.
 *
 * `$lib/work/queue` owns the reading and `queue.test.ts` holds it. What this
 * file pins is that the PAGE asks it, and that the pane renders a real link
 * per entry — there is no component renderer for a route in this suite (see
 * list-geometry.test.ts), so the assertions are against the page source.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { QUEUE_CAP } from '$lib/work/queue';

const page = readFileSync(new URL('./+page.svelte', import.meta.url), 'utf8');
const markup = page.slice(page.indexOf('</script>')).replace(/<!--[\s\S]*?-->/g, '');
/** The detail pane: everything from its landmark to the end of the page. */
const pane = markup.slice(markup.indexOf('aria-label="Item detail"'));
/**
 * The pane with no selection — what this issue is about. Anchored on the
 * section's own landmark rather than on the first `{:else}` after the pane,
 * which since #2246 is the one choosing between the entry block and the band.
 */
const empty = pane.slice(pane.indexOf('aria-label="Needs you"'));

describe('the empty detail pane', () => {
  it('reads the queue through the shared model, not its own filter', () => {
    expect(page).toMatch(/import \{ needsYouSummary \} from '\$lib\/work\/queue'/);
    expect(page).toMatch(/\$derived\(needsYouSummary\(rows, dashboard, clock\)\)/);
    // Not a second count of the same set beside the header's.
    expect(empty).not.toMatch(/\.filter\([^)]*needsYou/);
  });

  it('prints the queue headline where the one sentence used to be', () => {
    expect(empty).toContain('{queueHeadline}');
    // Which is the queue's own headline once the list has been read: only the
    // seconds before that (#2545 D9) say it is still being read rather than
    // "not read".
    expect(page).toMatch(/reading && !queue\.read \? 'Reading the needs-you queue…' : queue\.headline/);
  });

  it('gives every listed wait a link that can be acted on', () => {
    expect(empty).toMatch(/\{#each queue\.entries as entry \(entry\.key\)\}/);
    expect(empty).toContain('{entry.text}');
    // And it stays on the Work screen: since #2246 every wait selects in
    // place, so no anchor in this pane opens a new tab.
    expect(empty).not.toContain('target=');
    expect(empty).not.toContain('rel=');
  });

  it('routes a wait that selects an item through the list’s own href', () => {
    // `QueueEntry.href` is `/work?item=<id>`, with no query of its own: a click
    // from `?filter=needs-you` would have silently reset the list behind the
    // pane to the unfiltered view. `itemHref` is the same href the list's rows
    // take, which is the current URL with `?item=` set.
    expect(empty).toContain('href={entry.selects ? itemHref(entry.key) : entry.href}');
    expect(empty).toContain('onclick={entry.selects ? (event) => select(event, entry.key)');
  });

  it('says how many waits it did not list', () => {
    expect(empty).toMatch(/\{#if queue\.more > 0\}/);
    expect(empty).toContain('{queue.more}');
  });

  it('spends no magenta on a clear queue, or one nobody could read', () => {
    // The UI's colour ration: the needs-you mark means a person is the next
    // move. `NeedsYou.svelte` draws no band at all for the same reason.
    const mark = empty.indexOf('kind="needs_you"');
    expect(mark).toBeGreaterThan(-1);
    const guard = empty.lastIndexOf('{#if queue.entries.length > 0}', mark);
    expect(guard).toBeGreaterThan(-1);
    expect(empty.indexOf('{/if}', guard)).toBeLessThan(empty.indexOf('{queueHeadline}'));
  });

  it('still tells the reader what selecting a row will do', () => {
    // The old sentence is the pane's other half, not its whole content: the
    // summary says what is waiting, the sentence says what the list is for.
    expect(empty).toContain('Select an issue');
  });

  it('expands the one block when exactly one thing needs a person', () => {
    // #2280 D11. The first screen listed #2055 under a cyan "in review" mark
    // with no summary and no age, over "Select an issue to see what it is
    // waiting for" — the tab's whole question answered by a list of one and an
    // instruction. With exactly one wait the block itself opens here, Copy
    // button and all, and the instruction stands down.
    expect(empty).toContain('{#if soleBlock}');
    expect(empty).toContain('<NeedsYouBlock block={soleBlock} {copied} oncopy={copySelected} itemId={queue.sole?.itemId ?? null} />');
    expect(page).toContain('queue.sole === null ? null : needsYouBlock(queue.sole, clock)');
    // The sentence is the other branch's, not a line under an open block.
    const open = empty.indexOf('{#if soleBlock === null}');
    expect(open).toBeGreaterThan(-1);
    expect(open).toBeLessThan(empty.indexOf('Select an issue'));
  });

  it('marks every listed wait as a person`s move, with its sentence and age', () => {
    // The cyan "in review" mark said the machinery was working on a row whose
    // whole point is that it is not. Every row in THIS list needs a person, so
    // every row takes the one mark that means that, and carries the sentence
    // and the stamp the list beside it has no room for (#2280 D11).
    expect(empty).toContain('<StatusMark kind="needs_you" size={10} label="needs you" />');
    expect(empty).not.toContain('vocabulary="run"');
    expect(empty).toContain('{entry.summary}');
    expect(empty).toContain('{entry.since}');
  });

  it('keeps the listing short enough to read at a glance', () => {
    expect(QUEUE_CAP).toBeLessThanOrEqual(8);
  });
});
